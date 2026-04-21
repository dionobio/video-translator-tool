"""
Video Translator Tool - Video Downloader
Downloads videos via j2download.com API (supports Douyin, RedNote, TikTok, YouTube, Bilibili, etc.)
Falls back to yt-dlp if j2download fails.
"""

import os
import re
import hashlib
import subprocess
import tempfile
import urllib.request
import urllib.error
import json
import time


class J2DownloadClient:
    """Client for j2download.com API with PoW solver."""

    BASE_URL = "https://j2download.com"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.access_token = None

    def _solve_pow_classic(self, challenge, nonce, difficulty):
        """
        Solve Proof-of-Work challenge (classic mode).
        Hash format: pow:{challenge}:{counter}:{nonce}:{challenge_length}
        Must find counter where SHA-256 hash has `difficulty` leading zero hex nibbles.
        """
        challenge_len = str(len(challenge))
        prefix = f"pow:{challenge}:".encode()
        suffix = f":{nonce}:{challenge_len}".encode()

        for counter in range(100_000_000):
            data = prefix + str(counter).encode() + suffix
            hash_bytes = hashlib.sha256(data).digest()

            if self._has_leading_zero_nibbles(hash_bytes, difficulty):
                return str(counter)

        raise RuntimeError("PoW: không tìm được solution trong 100M lần thử")

    def _has_leading_zero_nibbles(self, hash_bytes, difficulty):
        """Check if hash has `difficulty` leading zero hex nibbles."""
        full_bytes = difficulty // 2
        for i in range(full_bytes):
            if hash_bytes[i] != 0:
                return False
        if difficulty % 2 == 1:
            if (hash_bytes[full_bytes] & 0xF0) != 0:
                return False
        return True

    def _fetch_bootstrap(self):
        """Fetch the page HTML and extract __BOOTSTRAP__ data."""
        req = urllib.request.Request(
            self.BASE_URL,
            headers={
                "User-Agent": self.USER_AGENT,
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Extract window.__BOOTSTRAP__ = {...}
        match = re.search(r'window\.__BOOTSTRAP__\s*=\s*(\{[^}]+\})', html)
        if not match:
            raise RuntimeError("Không tìm thấy __BOOTSTRAP__ data từ j2download.com")

        return json.loads(match.group(1))

    def _get_access_token(self, progress_callback=None):
        """Get JWT access token by solving PoW and calling auth endpoint."""
        if progress_callback:
            progress_callback("j2download: Đang lấy token xác thực...")

        bootstrap = self._fetch_bootstrap()
        nonce = bootstrap["nonce"]
        challenge = bootstrap.get("powChallenge", "")
        challenge_type = bootstrap.get("challengeType", "classic")
        difficulty = bootstrap.get("powDifficulty", 3)

        if progress_callback:
            progress_callback(f"j2download: Đang giải PoW (difficulty={difficulty})...")

        # Solve PoW
        if challenge:
            if challenge_type == "classic":
                solution = self._solve_pow_classic(challenge, nonce, difficulty)
            else:
                raise RuntimeError(f"PoW challenge type '{challenge_type}' không được hỗ trợ")
        else:
            solution = ""

        if progress_callback:
            progress_callback("j2download: PoW solved, đang xác thực...")

        # Request access token
        headers = {
            "User-Agent": self.USER_AGENT,
            "Content-Type": "application/json",
            "X-Page-Nonce": nonce,
            "Origin": self.BASE_URL,
            "Referer": self.BASE_URL + "/",
        }
        if solution:
            headers["X-Pow-Solution"] = solution

        req = urllib.request.Request(
            f"{self.BASE_URL}/api/auth/issue",
            data=b"null",
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if "accessToken" in result:
            self.access_token = result["accessToken"]
            return self.access_token
        else:
            raise RuntimeError(f"j2download auth thất bại: {result}")

    def get_download_links(self, video_url, progress_callback=None):
        """
        Get download links for a video URL.
        Returns dict with: author, title, duration, medias[], source, thumbnail, etc.
        """
        if not self.access_token:
            self._get_access_token(progress_callback)

        if progress_callback:
            progress_callback(f"j2download: Đang lấy link tải cho {video_url}...")

        body = json.dumps({"data": {"url": video_url, "unlock": True}}).encode()

        req = urllib.request.Request(
            f"{self.BASE_URL}/api/autolink",
            data=body,
            headers={
                "User-Agent": self.USER_AGENT,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "Origin": self.BASE_URL,
                "Referer": self.BASE_URL + "/",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if result.get("error"):
            msg = result.get("message", "Unknown error")
            raise RuntimeError(f"j2download lỗi: {msg}")

        return result


class VideoDownloader:
    """Download videos from various platforms via j2download.com or yt-dlp."""

    def __init__(self, output_dir=None):
        self.output_dir = output_dir or tempfile.mkdtemp(prefix="vtool_")
        os.makedirs(self.output_dir, exist_ok=True)

    def download(self, url, progress_callback=None):
        """
        Download video from URL.
        Tries j2download.com first, falls back to yt-dlp.
        Returns path to downloaded video file.
        """
        # Try j2download first
        try:
            return self._download_via_j2download(url, progress_callback)
        except Exception as e:
            if progress_callback:
                progress_callback(f"j2download thất bại: {e}")
                progress_callback("Đang thử tải bằng yt-dlp...")

        # Fallback to yt-dlp
        return self._download_via_ytdlp(url, progress_callback)

    def _download_via_j2download(self, url, progress_callback=None):
        """Download video using j2download.com API."""
        client = J2DownloadClient()
        info = client.get_download_links(url, progress_callback)

        # Find the best video media
        medias = info.get("medias", [])
        video_media = None
        for m in medias:
            if m.get("type") == "video":
                video_media = m
                break

        if not video_media:
            raise RuntimeError("Không tìm thấy video trong kết quả j2download")

        download_url = video_media["url"]
        ext = video_media.get("extension", "mp4")

        # Generate filename from title
        title = info.get("title", "video")
        # Clean title for filename
        title = re.sub(r'[<>:"/\\|?*#\[\]]', '', title)
        title = title.strip()[:50] or "video"
        filename = f"{title}.{ext}"
        filepath = os.path.join(self.output_dir, filename)

        if progress_callback:
            progress_callback(f"Đang tải: {title}")

        # Download the video file
        req = urllib.request.Request(
            download_url,
            headers={
                "User-Agent": J2DownloadClient.USER_AGENT,
                "Referer": "https://j2download.com/",
            },
        )

        with urllib.request.urlopen(req, timeout=300) as resp:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 1024  # 1MB

            with open(filepath, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        pct = downloaded * 100 // total_size
                        size_mb = downloaded / (1024 * 1024)
                        progress_callback(f"Đang tải: {size_mb:.1f} MB ({pct}%)")

        if progress_callback:
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            progress_callback(f"Tải xong: {os.path.basename(filepath)} ({size_mb:.1f} MB)")

        return filepath

    def _download_via_ytdlp(self, url, progress_callback=None):
        """Fallback: download video using yt-dlp."""
        output_template = os.path.join(self.output_dir, "%(title).50s.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", output_template,
            "--print", "after_move:filepath",
        ]

        # Platform-specific options
        if "douyin.com" in url or "iesdouyin.com" in url:
            cmd.extend(["--extractor-args", "douyinvod:referer=https://www.douyin.com/"])
        elif "xiaohongshu.com" in url or "xhslink.com" in url:
            cmd.extend([
                "--user-agent",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            ])

        cmd.append(url)

        if progress_callback:
            progress_callback("yt-dlp: Đang tải video...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Tải video thất bại:\n{result.stderr}")

        filepath = result.stdout.strip().split("\n")[-1].strip()

        if not os.path.exists(filepath):
            files = [
                os.path.join(self.output_dir, f)
                for f in os.listdir(self.output_dir)
                if f.endswith(".mp4")
            ]
            if files:
                filepath = max(files, key=os.path.getmtime)
            else:
                raise RuntimeError("Không tìm thấy file video sau khi tải.")

        if progress_callback:
            progress_callback(f"Tải xong: {os.path.basename(filepath)}")

        return filepath
