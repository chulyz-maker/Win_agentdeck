"""민감 정보 저장소 — Windows 자격 증명 관리자 (keyring).

- 계정 · 비밀번호 · 토큰 · 비공개 일정 주소는 셋업 창에서 입력하고 여기에만 저장한다.
  (Windows 로그인 계정으로 암호화되며, 코드 · 설정 파일 · exe 안에는 들어가지 않는다.)
- 확인: 제어판 → 자격 증명 관리자 → Windows 자격 증명 → "CharlieDeck" 항목
- 저장 · 삭제할 때마다 설정 폴더의 secrets.stamp(시각만 기록)를 바꿔 실행 중인 CharlieDeck 이 다시 읽게 한다.

테스트 · 개발용: 환경 변수 CHARLIEDECK_SECRETS=memory 이면 메모리에만 저장한다.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from platform_util import config_dir

SERVICE = "CharlieDeck"

# key → (셋업 창 이름, 다시 보여주지 않음)
KEYS: dict[str, tuple[str, bool]] = {
    "hiworks.user": ("하이웍스 메일 계정", False),
    "hiworks.password": ("하이웍스 메일 비밀번호", True),
    "google.ical_url": ("Google 비공개 iCal 주소", True),
    "todoist.token": ("Todoist API 토큰", True),
    "ecount.company": ("이카운트 회사코드", False),
    "ecount.user": ("이카운트 아이디", False),
    "ecount.password": ("이카운트 비밀번호", True),
}


class VaultUnavailable(RuntimeError):
    pass


class MemoryBackend:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.data.get(f"{service}/{key}")

    def set_password(self, service: str, key: str, value: str) -> None:
        self.data[f"{service}/{key}"] = value

    def delete_password(self, service: str, key: str) -> None:
        self.data.pop(f"{service}/{key}", None)


def _keyring_backend():
    try:
        import keyring  # noqa: PLC0415
        from keyring.backends import fail  # noqa: PLC0415
    except ImportError as e:
        raise VaultUnavailable("keyring 패키지가 없어요: pip install keyring") from e
    if isinstance(keyring.get_keyring(), fail.Keyring):
        raise VaultUnavailable("이 PC 에서 자격 증명 관리자를 쓸 수 없어요")
    return keyring


class Vault:
    def __init__(self, backend=None, stamp: Path | None = None) -> None:
        if backend is None:
            backend = MemoryBackend() if os.environ.get("CHARLIEDECK_SECRETS") == "memory" else _keyring_backend()
        self.backend = backend
        self.stamp = stamp if stamp is not None else config_dir() / "secrets.stamp"
        self._lock = threading.Lock()

    def get(self, key: str) -> str:
        try:
            return self.backend.get_password(SERVICE, key) or ""
        except Exception:  # 자격 증명 관리자 오류: 값이 없는 것으로 본다
            return ""

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    def set(self, key: str, value: str) -> None:
        if key not in KEYS:
            raise KeyError(key)
        value = (value or "").strip()
        with self._lock:
            if value:
                self.backend.set_password(SERVICE, key, value)
            else:
                self._delete(key)
            self.touch()

    def delete(self, key: str) -> None:
        with self._lock:
            self._delete(key)
            self.touch()

    def _delete(self, key: str) -> None:
        try:
            self.backend.delete_password(SERVICE, key)
        except Exception:  # 없는 항목 지우기 등
            pass

    def values(self) -> list[str]:
        """저장된 비밀 값들 (로그 · 오류 문구에서 가리기용). 화면에 쓰지 않는다."""
        return [v for k, (_, hidden) in KEYS.items() if hidden and (v := self.get(k))]

    def touch(self) -> None:
        try:
            self.stamp.parent.mkdir(parents=True, exist_ok=True)
            self.stamp.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass

    def stamp_mtime(self) -> float:
        try:
            return self.stamp.stat().st_mtime
        except OSError:
            return 0.0


def mask(text: str, secrets: list[str]) -> str:
    """오류 문구에 비밀 값이 섞여 있으면 *** 로 가린다."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text
