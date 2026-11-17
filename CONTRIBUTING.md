# 기여하기

CharlieDeck 은 Windows 11 + Ulanzi D200 개인 업무 대시보드예요. 버그 제보와 개선 제안을 환영합니다.

## 개발 환경

```bat
scripts\windows\setup.bat
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest -q tests
.venv\Scripts\python.exe -m charliedeck --demo --sim
```

리눅스에서도 테스트 · PNG 출력(`--snapshot`)은 돌아가요 (`CHARLIEDECK_SECRETS=memory`).

## 원칙

- **비밀 값은 코드 · 설정 파일 · 로그에 넣지 않아요.** 계정 · 비밀번호 · 토큰 · 비공개 주소는 `vault.py`(Windows 자격 증명 관리자)로만.
- 회사마다 다른 주소는 코드에 넣지 않고 설정 창에서 입력받아요.
- 업무 브라우저는 **읽기만** 해요. 새로 누르는 단추가 필요하면 PR 에 이유를 적어 주세요.
- 화면 문구는 한국어, 버튼은 196×196 · 넓은 칸은 458×196 PNG.
- 새 기능에는 장치 · 네트워크 없이 도는 테스트를 같이 넣어 주세요.

## 코드 구조

[docs/CHARLIEDECK.md](docs/CHARLIEDECK.md#8-코드-구조) 참고.
