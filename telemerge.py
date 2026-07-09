import asyncio
import io
import logging
import re
import hashlib
import sys
from pathlib import Path

import yaml
from telethon import TelegramClient, events
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
)

# ─── Config ───────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Logger ───────────────────────────────────────────────────────

def setup_logger(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("telemerge")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


# ─── Filter ───────────────────────────────────────────────────────

class MessageFilter:
    def __init__(self, include: list[str], exclude: list[str]):
        self.include_patterns = [re.compile(kw, re.IGNORECASE) for kw in include] if include else []
        self.exclude_patterns = [re.compile(kw, re.IGNORECASE) for kw in exclude] if exclude else []

    def should_forward(self, text: str | None) -> bool:
        if not text:
            # 미디어만 있는 메시지는 include 필터가 없으면 통과
            return len(self.include_patterns) == 0

        # exclude 먼저 체크
        for pattern in self.exclude_patterns:
            if pattern.search(text):
                return False

        # include가 비어있으면 전체 통과
        if not self.include_patterns:
            return True

        # include 중 하나라도 매칭되면 통과
        return any(p.search(text) for p in self.include_patterns)


# ─── Aggregator ───────────────────────────────────────────────────

class TelegramAggregator:
    CONCURRENT_FORWARDS = 5  # 동시 전달 worker 수

    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logger(config.get("log_level", "INFO"))
        self._queue: asyncio.Queue = asyncio.Queue()
        self._forwarded: set[tuple[int, int]] = set()  # (chat_id, msg_id)
        self._poll_interval = config.get("poll_interval", 300)  # 기본 5분
        self._poll_limit = config.get("poll_limit", 100)  # 폴링 시 스캔할 메시지 수
        self.client = TelegramClient(
            "telemerge_session",
            config["api_id"],
            config["api_hash"],
        )
        self.msg_filter = MessageFilter(
            include=config.get("filters", {}).get("include", []),
            exclude=config.get("filters", {}).get("exclude", []),
        )
        self.bot_token = config.get("bot_token") or None
        if self.bot_token:
            self.bot_client = TelegramClient(
                "bot_session",
                config["api_id"],
                config["api_hash"],
            )
        else:
            self.bot_client = None
        self.forward_media = config.get("forward_media", True)
        self.source_names: dict[int, str] = {}  # chat_id → display name 캐시

    def _get_message_signature(self, message) -> str:
        """메시지의 고유 식별자(Signature)를 생성합니다."""
        # 1. 텔레그램 네이티브 포워딩인 경우
        if message.fwd_from and message.fwd_from.from_id:
           from_id = getattr(message.fwd_from.from_id, 'channel_id', None) or \
                     getattr(message.fwd_from.from_id, 'user_id', None)
           orig_msg_id = getattr(message.fwd_from, 'channel_post', None) or \
                         getattr(message.fwd_from, 'saved_from_msg_id', None)
           if from_id and orig_msg_id:
               return f"fwd:{from_id}:{orig_msg_id}"

        # # 2. 미디어가 있는 경우 우선 처리 (동일 이미지/파일 + 다른 코멘트 중복 방지)
        # if message.media:
        #    if hasattr(message.media, 'document') and message.media.document:
        #        return f"media:{message.media.document.id}"
        #    if hasattr(message.media, 'photo') and message.media.photo:
        #        return f"media:{message.media.photo.id}"
        #    # MessageMediaWebPage(링크 프리뷰) 등은 미디어 서명에서 제외하고 텍스트로 넘김
        #
        # # 3. 미디어가 없는 텍스트 단독 메시지인 경우
        # text = message.text or message.message or ""
        # if text:
        #     normalized_text = re.sub(r'\s+', '', text).strip()
        #     text_hash = hashlib.md5(normalized_text.encode('utf-8')).hexdigest()
        #     return f"text:{text_hash}"

        # 4. 위 조건에 해당하지 않는 경우
        return f"local:{message.chat_id}:{message.id}"

    async def resolve_entity(self, source):
        """소스를 entity로 변환하고 캐시"""
        entity = await self.client.get_entity(source)
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
        # 두 가지 ID 형식 모두 캐시 (event.chat_id와 entity.id가 다를 수 있음)
        self.source_names[entity.id] = title
        if isinstance(source, int):
            self.source_names[source] = title
        # -100 붙인/뗀 변환도 캐시
        self.source_names[-(1000000000000 + entity.id)] = title
        self.source_names[int(f"-100{entity.id}")] = title
        self.logger.info(f"소스 등록: {title} (ID: {entity.id})")
        return entity

    async def resolve_destination(self, dest_config: dict):
        """목적지 설정 해석 (봇 사용 시 봇 클라이언트로 resolve)"""
        target = dest_config["target"]
        if target == "me":
            return "me"
        client = self.bot_client or self.client
        return await client.get_entity(target)

    @staticmethod
    def _normalize_chat_id(chat_id: int) -> int:
        """chat_id를 raw entity ID 포맷으로 정규화"""
        from telethon.utils import resolve_id
        resolved_id, _ = resolve_id(chat_id)
        return resolved_id

    @staticmethod
    def _extract_media_info(media) -> tuple[str | None, bool]:
        """원본 미디어에서 파일명과 문서 여부를 추출"""
        if isinstance(media, MessageMediaPhoto):
            return "photo.jpg", False

        if isinstance(media, MessageMediaDocument):
            doc = media.document
            # 비디오 속성이 있으면 항상 인라인 재생 (원본 의도 보존)
            has_video_attr = any(
                isinstance(attr, DocumentAttributeVideo) for attr in doc.attributes
            )
            if has_video_attr:
                # 원본 파일명이 있으면 유지, 없으면 기본값
                file_name = None
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeFilename):
                        file_name = attr.file_name
                        break
                return file_name or "video.mp4", False
            # 원본 파일명 추출
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name, True
            # 파일명이 없으면 MIME 타입에서 확장자 추론
            mime = getattr(doc, "mime_type", "") or ""
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp", "video/mp4": ".mp4", "audio/mpeg": ".mp3",
                "audio/ogg": ".ogg", "application/pdf": ".pdf",
            }
            ext = ext_map.get(mime, "")
            if ext:
                base = mime.split("/")[0]  # image, video, audio 등
                return f"{base}{ext}", base not in ("image", "video")
            return None, True

        return None, True

    def _build_message_link(self, chat_id: int, message_id: int) -> str:
        """원본 메시지 링크 생성"""
        # 비공개 채널: t.me/c/{channel_id}/{msg_id}
        # -100 prefix 제거
        raw_id = str(chat_id).replace("-100", "")
        return f"https://t.me/c/{raw_id}/{message_id}"

    async def _send_message(self, dest, message, chat_id: int):
        """단일 메시지를 목적지로 전송 (텍스트 복사 + 원본 링크)"""
        sender = self.bot_client or self.client
        text = message.text or message.message or ""
        name = self.source_names.get(chat_id, str(chat_id))
        link = self._build_message_link(chat_id, message.id)
        source_tag = f"\n\n[from {name}]({link})"

        has_media = message.media and not isinstance(
            message.media, MessageMediaWebPage
        )

        if has_media and self.forward_media:
            caption = f"{text}{source_tag}"
            if len(caption) > 1024:
                caption = caption[: 1024 - len(source_tag) - 3] + f"...{source_tag}"
            if self.bot_client:
                file_bytes = await self.client.download_media(message, file=bytes)
                if file_bytes:
                    file_name, force_document = self._extract_media_info(message.media)
                    file_io = io.BytesIO(file_bytes)
                    file_io.name = file_name or "file"
                    await sender.send_file(
                        dest,
                        file=file_io,
                        caption=caption,
                        parse_mode="md",
                        force_document=force_document,
                    )
                else:
                    full_text = f"{text}{source_tag}"
                    if full_text.strip():
                        await sender.send_message(dest, full_text, parse_mode="md")
            else:
                await sender.send_file(
                    dest,
                    file=message.media,
                    caption=caption,
                    parse_mode="md",
                )
        else:
            full_text = f"{text}{source_tag}"
            if full_text.strip():
                await sender.send_message(dest, full_text, parse_mode="md")

    async def _forward_worker(self, worker_id: int):
        """큐에서 메시지를 꺼내 동시 처리하는 worker"""
        while True:
            try:
                message, chat_id, destinations = await self._queue.get()
                if isinstance(message, list):
                    await self.forward_album(message, chat_id, destinations)
                else:
                    await self.forward_message(message, chat_id, destinations)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"worker-{worker_id} 처리 실패: {e}")
            finally:
                self._queue.task_done()

    async def forward_album(self, messages: list, chat_id: int, destinations):
        """앨범(미디어 그룹)을 목적지로 전달"""
        # 첫 메시지의 캡션으로 필터 체크
        first = messages[0]
        text = first.text or first.message or ""
        if not self.msg_filter.should_forward(text):
            self.logger.debug(f"필터에 의해 앨범 스킵: {text[:50]}...")
            for msg in messages:
                self._forwarded.add((chat_id, msg.id))
            return

        # 글로벌 중복 검사 (앨범은 첫 번째 메시지 기준으로 식별)
        signature = self._get_message_signature(first)
        if signature in self._seen_signatures:
            self.logger.info(f"타 채널에서 이미 수집된 중복 앨범 스킵: {self.source_names.get(chat_id, chat_id)}")
            for msg in messages:
                self._forwarded.add((chat_id, msg.id))
            return

        # 사전 예약 (Lock)
        self._seen_signatures.add(signature)
        for msg in messages:
            self._forwarded.add((chat_id, msg.id))

        forwarded = False
        try:
            for dest in destinations:
                if self.bot_client:
                    try:
                        await self._send_album(dest, messages, chat_id)
                        self.logger.info(
                            f"앨범 전달 완료 (봇): {self.source_names.get(chat_id, chat_id)} → {len(messages)}장"
                        )
                        forwarded = True
                    except Exception as e:
                        self.logger.error(f"봇 앨범 전달 실패 ({dest}): {e}")
                else:
                    try:
                        await self.client.forward_messages(dest, messages)
                        self.logger.info(
                            f"앨범 전달 완료 (네이티브): {self.source_names.get(chat_id, chat_id)} → {len(messages)}장"
                        )
                        forwarded = True
                    except Exception as e:
                        self.logger.warning(f"네이티브 앨범 전달 실패, fallback 시도: {e}")
                        try:
                            await self._send_album(dest, messages, chat_id)
                            self.logger.info(
                                f"앨범 전달 완료 (fallback): {self.source_names.get(chat_id, chat_id)} → {len(messages)}장"
                            )
                            forwarded = True
                        except Exception as e2:
                            self.logger.error(f"fallback 앨범 전달도 실패 ({dest}): {e2}")

                if forwarded:
                    await self.client.send_read_acknowledge(chat_id, messages[-1])
        finally:
            # 모든 전송이 실패한 경우에만 예약 해제 (Rollback)
            if not forwarded:
                self._seen_signatures.discard(signature)
                for msg in messages:
                    self._forwarded.discard((chat_id, msg.id))
                self.logger.warning(f"앨범 전송 전면 실패, 서명 롤백: {signature}")

    async def _send_album(self, dest, messages: list, chat_id: int):
        """앨범을 목적지로 전송 (미디어 다운로드 → 일괄 전송)"""
        sender = self.bot_client or self.client

        # 첫 메시지의 캡션 + 원본 링크
        first = messages[0]
        text = first.text or first.message or ""
        name = self.source_names.get(chat_id, str(chat_id))
        link = self._build_message_link(chat_id, first.id)
        source_tag = f"\n\n[from {name}]({link})"
        caption = f"{text}{source_tag}"
        if len(caption) > 1024:
            caption = caption[: 1024 - len(source_tag) - 3] + f"...{source_tag}"

        files = []
        captions = []
        force_documents = []

        for i, msg in enumerate(messages):
            has_media = msg.media and not isinstance(msg.media, MessageMediaWebPage)
            if not has_media:
                continue

            if self.bot_client:
                file_bytes = await self.client.download_media(msg, file=bytes)
                if not file_bytes:
                    continue
                file_name, force_doc = self._extract_media_info(msg.media)
                file_io = io.BytesIO(file_bytes)
                file_io.name = file_name or "file"
                files.append(file_io)
            else:
                files.append(msg.media)
                _, force_doc = self._extract_media_info(msg.media)

            # 캡션은 첫 번째 파일에만 부여 (Telegram 앨범 규칙)
            captions.append(caption if len(files) == 1 else "")
            force_documents.append(force_doc)

        if not files:
            # 미디어가 없으면 텍스트만 전송
            full_text = f"{text}{source_tag}"
            if full_text.strip():
                await sender.send_message(dest, full_text, parse_mode="md")
            return

        # 모두 문서인지 확인 (혼합 시 문서 모드 사용하지 않음)
        all_docs = all(force_documents)
        await sender.send_file(
            dest,
            file=files,
            caption=captions,
            parse_mode="md",
            force_document=all_docs,
        )

    async def forward_message(self, message, chat_id: int, destinations):
        """메시지를 목적지로 전달 (네이티브 포워딩 → 실패 시 텍스트 복사 fallback)"""
        text = message.text or message.message or ""

        if not self.msg_filter.should_forward(text):
            self.logger.debug(f"필터에 의해 스킵: {text[:50]}...")
            self._forwarded.add((chat_id, message.id))
            return

        # 글로벌 중복 검사
        signature = self._get_message_signature(message)
        if signature in self._seen_signatures:
            self.logger.info(f"타 채널에서 이미 수집된 중복 메시지 스킵: {self.source_names.get(chat_id, chat_id)}")
            return

        # 사전 예약 (Lock)
        self._seen_signatures.add(signature)
        self._forwarded.add((chat_id, message.id))

        forwarded = False
        try:
            for dest in destinations:
                if self.bot_client:
                    try:
                        await self._send_message(dest, message, chat_id)
                        self.logger.info(
                            f"전달 완료 (봇): {self.source_names.get(chat_id, chat_id)}"
                        )
                        forwarded = True
                    except Exception as e:
                        self.logger.error(f"봇 전달 실패 ({dest}): {e}")
                else:
                    try:
                        await self.client.forward_messages(dest, message)
                        self.logger.info(
                            f"전달 완료 (네이티브): {self.source_names.get(chat_id, chat_id)}"
                        )
                        forwarded = True
                    except Exception as e:
                        self.logger.warning(f"네이티브 전달 실패, fallback 시도: {e}")
                        try:
                            await self._send_message(dest, message, chat_id)
                            self.logger.info(
                                f"전달 완료 (fallback): {self.source_names.get(chat_id, chat_id)}"
                            )
                            forwarded = True
                        except Exception as e2:
                            self.logger.error(f"fallback 전달도 실패 ({dest}): {e2}")

                if forwarded:
                    await self.client.send_read_acknowledge(chat_id, message)
        finally:
            # 모든 전송이 실패한 경우에만 예약 해제 (Rollback)
            if not forwarded:
                self._seen_signatures.discard(signature)
                self._forwarded.discard((chat_id, message.id))
                self.logger.warning(f"메시지 전송 전면 실패, 서명 롤백: {signature}")

    async def _poll_missed(self, source_ids: list[int], destinations):
        """주기적으로 소스 채널을 확인하여 누락된 메시지를 포워딩"""
        while True:
            await asyncio.sleep(self._poll_interval)
            for chat_id in source_ids:
                try:
                    # 메시지를 모아서 grouped_id로 분류
                    solo_msgs = []
                    album_groups: dict[int, list] = {}

                    async for msg in self.client.iter_messages(chat_id, limit=self._poll_limit):
                        if (chat_id, msg.id) in self._forwarded:
                            continue
                        if msg.grouped_id is not None:
                            album_groups.setdefault(msg.grouped_id, []).append(msg)
                        else:
                            solo_msgs.append(msg)

                    # 개별 메시지 큐잉
                    for msg in solo_msgs:
                        text = msg.text or msg.message or ""
                        if not self.msg_filter.should_forward(text):
                            self._forwarded.add((chat_id, msg.id))
                            continue
                        self.logger.info(
                            f"누락 메시지 발견: {self.source_names.get(chat_id, chat_id)}/{msg.id}"
                        )
                        self._forwarded.add((chat_id, msg.id))
                        await self._queue.put((msg, chat_id, destinations))

                    # 앨범 그룹 큐잉
                    for grouped_id, msgs in album_groups.items():
                        # iter_messages는 역순(최신 먼저)이므로 정렬
                        msgs.sort(key=lambda m: m.id)
                        self.logger.info(
                            f"누락 앨범 발견: {self.source_names.get(chat_id, chat_id)}/group={grouped_id} ({len(msgs)}장)"
                        )
                        for msg in msgs:
                            self._forwarded.add((chat_id, msg.id))
                        await self._queue.put((msgs, chat_id, destinations))
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    self.logger.error(f"폴링 실패 ({chat_id}): {e}")

    async def run(self):
        """메인 실행"""
        # catch_up → 연결 끊김 중 놓친 업데이트를 재접속 시 복구
        await self.client.start()
        await self.client.catch_up()
        me = await self.client.get_me()
        self.logger.info(f"로그인 완료: {me.first_name} (@{me.username})")

        # 봇 클라이언트 시작
        if self.bot_client:
            await self.bot_client.start(bot_token=self.bot_token)
            bot_me = await self.bot_client.get_me()
            self.logger.info(f"봇 클라이언트 로그인 완료: @{bot_me.username}")

        # 소스 채널 등록
        source_entities = []
        for src in self.config["sources"]:
            try:
                entity = await self.resolve_entity(src)
                source_entities.append(entity)
            except Exception as e:
                self.logger.error(f"소스 등록 실패 ({src}): {e}")

        if not source_entities:
            self.logger.error("등록된 소스가 없습니다. 종료합니다.")
            return

        # 목적지 등록
        destinations = []
        for dest_conf in self.config["destinations"]:
            try:
                dest = await self.resolve_destination(dest_conf)
                destinations.append(dest)
                self.logger.info(f"목적지 등록: {dest_conf['target']}")
            except Exception as e:
                self.logger.error(f"목적지 등록 실패 ({dest_conf}): {e}")

        if not destinations:
            self.logger.error("등록된 목적지가 없습니다. 종료합니다.")
            return

        # 소스 채널 ID 리스트
        source_ids = [e.id for e in source_entities]

        # 기존 메시지 ID를 시드하여 폴러가 과거 메시지를 중복 전달하지 않도록 함
        for chat_id in source_ids:
            try:
                async for msg in self.client.iter_messages(chat_id, limit=self._poll_limit):
                    self._forwarded.add((chat_id, msg.id))
            except Exception as e:
                self.logger.warning(f"시드 실패 ({chat_id}): {e}")

        self.logger.info(
            f"집계 시작: {len(source_ids)}개 소스 → {len(destinations)}개 목적지"
        )

        # 이벤트 핸들러 등록 (큐에 넣기만 함)
        @self.client.on(events.NewMessage(chats=source_ids))
        async def handler(event):
            if event.message.grouped_id is not None:
                return  # Album 핸들러에서 처리
            chat_id = self._normalize_chat_id(event.chat_id)
            key = (chat_id, event.message.id)
            if key in self._forwarded:
                return
            self._forwarded.add(key)
            await self._queue.put((event.message, chat_id, destinations))

        @self.client.on(events.Album(chats=source_ids))
        async def album_handler(event):
            chat_id = self._normalize_chat_id(event.chat_id)
            keys = [(chat_id, m.id) for m in event.messages]
            if all(k in self._forwarded for k in keys):
                return
            for k in keys:
                self._forwarded.add(k)
            await self._queue.put((event.messages, chat_id, destinations))

        # 동시 처리 worker 시작
        workers = [
            asyncio.create_task(self._forward_worker(i))
            for i in range(self.CONCURRENT_FORWARDS)
        ]

        # 누락 메시지 폴러 시작
        poller = asyncio.create_task(self._poll_missed(source_ids, destinations))

        self.logger.info(
            f"메시지 대기 중... (worker {self.CONCURRENT_FORWARDS}개, "
            f"폴링 {self._poll_interval}초 간격, Ctrl+C로 종료)"
        )
        try:
            await self.client.run_until_disconnected()
        finally:
            poller.cancel()
            for w in workers:
                w.cancel()
            if self.bot_client:
                await self.bot_client.disconnect()


# ─── Entry ────────────────────────────────────────────────────────

async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    if not Path(config_path).exists():
        print(f"설정 파일을 찾을 수 없습니다: {config_path}")
        print("config.yaml.example을 config.yaml로 복사한 후 수정하세요.")
        sys.exit(1)

    config = load_config(config_path)
    aggregator = TelegramAggregator(config)
    await aggregator.run()


if __name__ == "__main__":
    asyncio.run(main())
