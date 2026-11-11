"""头像下载、无损 WebP 转码及原子文件存储，不信任远程图片的格式声明。"""

import asyncio
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
from uuid import uuid4
import warnings

import httpx
from PIL import Image, ImageOps

from app.config.avatar import AvatarSettings
from app.infrastructure.dingtalk_http import DingTalkHTTPClient


def avatar_filename(relative_url: str) -> str:
    if not re.fullmatch(r'user/avatar/[a-f0-9]{32}\.webp', relative_url):
        raise ValueError('头像存储地址不合法')
    return relative_url.rsplit('/', 1)[1]


def avatar_response_filename(relative_url: str | None) -> str | None:
    """将内部存储引用映射为登录响应中的头像文件名，不暴露目录或路由前缀。"""
    return avatar_filename(relative_url) if relative_url else None


def _webp_chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack('<I', len(payload)) + payload + (b'\0' if len(payload) % 2 else b'')


def _uint24(value: int) -> bytes:
    if not 0 <= value < 1 << 24:
        raise ValueError('WebP 动画参数超出表示范围')
    return value.to_bytes(3, 'little')


def _lossless_animation(frames: list[Image.Image], durations: list, loop: int, icc: bytes | None) -> bytes:
    # Pillow 动画编码接口未传递 exact；逐帧静态编码后按官方 RIFF 规范封装，保留透明 RGB。
    width, height = frames[0].size
    flags = 0x02 | (0x10 if any(frame.getextrema()[3][0] < 255 for frame in frames) else 0)
    flags |= 0x20 if icc else 0
    chunks = [_webp_chunk(b'VP8X', bytes([flags, 0, 0, 0]) + _uint24(width - 1) + _uint24(height - 1))]
    if icc:
        chunks.append(_webp_chunk(b'ICCP', icc))
    chunks.append(_webp_chunk(b'ANIM', b'\0' * 4 + struct.pack('<H', loop)))
    timestamp = 0.0
    for frame, duration in zip(frames, durations, strict=True):
        if frame.size != (width, height):
            raise ValueError('动画帧尺寸不一致，不能无损保存为完整画布')
        output = BytesIO()
        # method 只控制压缩计算量；保留无损及透明像素，避免首次机器人建档被高压缩耗时拖垮。
        frame.save(output, format='WEBP', lossless=True, quality=100, method=4, exact=True)
        static = output.getvalue()
        image_chunks = []
        offset = 12
        while offset < len(static):
            size = int.from_bytes(static[offset + 4:offset + 8], 'little')
            end = offset + 8 + size + size % 2
            if static[offset:offset + 4] == b'VP8L':
                image_chunks.append(static[offset:end])
            offset = end
        if len(image_chunks) != 1:
            raise ValueError('头像帧未生成唯一无损 WebP 数据块')
        next_timestamp = timestamp + duration
        frame_header = (b'\0' * 6 + _uint24(width - 1) + _uint24(height - 1)
                        + _uint24(round(next_timestamp) - round(timestamp)) + b'\x02')
        # 每帧覆盖完整画布且不做透明混合，避免合成时再次丢失透明像素的 RGB。
        chunks.append(_webp_chunk(b'ANMF', frame_header + image_chunks[0]))
        timestamp = next_timestamp
    body = b'WEBP' + b''.join(chunks)
    return b'RIFF' + struct.pack('<I', len(body)) + body


class AvatarStore:
    def __init__(self, settings: AvatarSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(4)

    def _validate_url(self, value: str) -> httpx.URL:
        url = httpx.URL(value)
        host = url.host.lower()
        if (url.scheme != 'https' or url.userinfo or url.port not in (None, 443)
                or not any(host == allowed or host.endswith('.' + allowed) for allowed in self.settings.allowed_hosts)):
            raise ValueError('头像地址必须属于允许的 HTTPS 域名')
        return url

    async def _download(self, value: str) -> bytes:
        url = self._validate_url(value)
        if self._client is None:
            self._client = DingTalkHTTPClient(proxy_url=self.settings.dingtalk_proxy_url, timeout=self.settings.timeout_seconds, transport=self._transport)
        for _ in range(4):
            async with self._client.stream('GET', url) as response:
                if response.is_redirect:
                    # 重定向的每一跳都检查域名，不把钉钉应用凭证发送给图片服务。
                    url = self._validate_url(str(url.join(response.headers['location'])))
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_download_bytes:
                        raise ValueError('头像下载超出大小限制')
                if not content:
                    raise ValueError('头像内容为空')
                return bytes(content)
        raise ValueError('头像重定向次数过多')

    def _encode(self, content: bytes) -> bytes:
        frames = []
        durations = []
        pixels = 0
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                if source.info.get('default_image'):
                    raise ValueError('带独立默认图像的动画不能直接无损转换')
                icc = source.info.get('icc_profile')
                loop = source.info.get('loop', 0)
                for index in range(getattr(source, 'n_frames', 1)):
                    source.seek(index)
                    pixels += source.width * source.height
                    if pixels > self.settings.max_pixels or source.width > 16383 or source.height > 16383:
                        raise ValueError('头像像素超出限制')
                    if source.mode not in ('1', 'L', 'LA', 'P', 'RGB', 'RGBA'):
                        raise ValueError('头像色彩模式无法无损转换为 WebP')
                    source.load()
                    frame = ImageOps.exif_transpose(source).convert('RGBA')
                    frames.append(frame)
                    durations.append(source.info.get('duration', 100))
                if len(frames) > 1:
                    encoded = _lossless_animation(frames, durations, loop, icc)
                else:
                    output = BytesIO()
                    # 适度压缩即可；method=6 对大头像可能耗时超过机器人整个回调预算。
                    options = dict(format='WEBP', lossless=True, quality=100, method=4, exact=True)
                    if icc:
                        options['icc_profile'] = icc
                    frames[0].save(output, **options)
                    encoded = output.getvalue()
                # 不缩放、不量化，保留透明像素；动态图保留全部帧。
                if len(encoded) > self.settings.max_download_bytes:
                    raise ValueError('无损 WebP 超出存储大小限制')
                return encoded

    def _write(self, filename: str, content: bytes) -> None:
        root = self.settings.root
        root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix='.avatar-', delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, root / filename)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def save_from_url(self, url: str) -> str:
        async with self._semaphore:
            return await self._save_from_url(url)

    async def _save_from_url(self, url: str) -> str:
        async with asyncio.timeout(self.settings.timeout_seconds):
            content = await self._download(url)
        encoded = await asyncio.to_thread(self._encode, content)
        relative_url = 'user/avatar/' + uuid4().hex + '.webp'
        task = asyncio.create_task(asyncio.to_thread(self._write, avatar_filename(relative_url), encoded))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 文件写入线程不能被取消；等待完成后清理本次未用于员工记录的文件。
            try:
                await task
            finally:
                await self.delete(relative_url)
            raise
        except BaseException:
            await self.delete(relative_url)
            raise
        return relative_url

    async def delete(self, relative_url: str) -> None:
        await asyncio.to_thread((self.settings.root / avatar_filename(relative_url)).unlink, missing_ok=True)

    async def read(self, relative_url: str) -> bytes:
        filename = avatar_filename(relative_url)
        def read_file():
            directory = os.open(self.settings.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, 'rb') as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise ValueError('头像必须为普通文件')
                    content = file.read(self.settings.max_download_bytes + 1)
                    if len(content) > self.settings.max_download_bytes:
                        raise ValueError('头像文件超出大小限制')
                    return content
            finally:
                os.close(directory)
        return await asyncio.to_thread(read_file)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
