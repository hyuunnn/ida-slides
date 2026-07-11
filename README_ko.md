<p align="center">
  <img src="assets/logo.png" alt="ida-slides — IDA 안에 도킹된 Marp / Slidev 슬라이드" width="760">
</p>

[English](README.md) | 한국어

# ida-slides

진짜 **Marp** 또는 **Slidev** 슬라이드 덱을 IDA Pro의 도킹 탭 안에서 띄웁니다 —
`@이름` 토큰은 클릭 가능한 링크로 렌더링되어 디스어셈블리 뷰를 해당 위치로
점프시킵니다.

슬라이드는 오른쪽에, 코드는 왼쪽에 두고 분석 내용을 발표하세요. 덱 어디에든
`@sub_401000`, `@main`, `@0x401000`처럼 쓰면 하이라이트된 링크가 되고, 클릭하면
IDA가 해당 함수/주소로 이동합니다.

## 사용법

1. `Ctrl+Shift+M` (또는 View → Open subviews → ida-slides: Open Slides…)
2. 마크다운 덱(`.md`) 선택

덱은 IDA 탭에 임베드된 네이티브 웹뷰로 렌더링됩니다 — macOS는 WKWebView,
Windows는 WebView2 — QtWebEngine이 필요 없습니다. 엔진은 덱마다 자동
선택됩니다:

- **Marp** (기본): 저장할 때마다 `marp` CLI가 HTML로 변환하고, 현재 슬라이드를
  유지한 채 뷰가 리로드됩니다. Marp 테마·배경·페이지 번호 완전 지원.
- **Slidev**: front matter에 Slidev 전용 키(`transition:`, `mdc:`,
  `drawings:` …)가 있으면 선택됩니다. ida-slides가 로컬 `slidev` 개발 서버를
  띄워 탭에 표시하며, Vite HMR이 저장 즉시 반영합니다.

front matter에 `ida-slides-engine: marp` 또는 `ida-slides-engine: slidev`를 넣으면
엔진을 강제할 수 있습니다. 조작은 각 도구의 기본 키 그대로입니다 (←/→, `f`
전체화면, Slidev의 `o` 오버뷰 등).

요구 사항:

- Marp: `npm i -g @marp-team/marp-cli`
- Slidev: `npm i -g @slidev/cli` (+ 덱이 쓰는 테마, 예:
  `@slidev/theme-default`)
- macOS: pyobjc-framework-WebKit (플러그인 매니저가 자동 설치; 수동:
  `pip install --user pyobjc-framework-WebKit`)
- Windows: WebView2 런타임 (Windows 10/11에 기본 탑재; 로더는 플러그인에
  포함된 `win/WebView2Loader.dll` — **x64 전용**. ARM64 IDA를 쓰거나
  로더가 구버전이 되면 Microsoft.Web.WebView2 NuGet 패키지에서 맞는
  바이너리로 교체하세요; 정확한 경로와 현재 버전은
  `win/PROVENANCE.txt` 참고)

CLI는 PATH, nvm/nvm-windows, npm 글로벌 bin, Homebrew, pnpm, scoop 순으로
탐색합니다. marp-cli로 내보낸 `.html` 파일도 직접 열 수 있습니다.

### 플랫폼 지원

**macOS와 Windows**를 지원합니다 — 덱은 각 플랫폼의 네이티브 웹뷰(macOS는
PyObjC 기반 WKWebView, Windows는 COM 기반 WebView2)로 렌더링되며,
렌더링에는 해당 엔진의 CLI 설치가 필요합니다. 폴백 뷰어는 없습니다: 다른
플랫폼이거나 marp/slidev가 없으면 플러그인은 로드되지만 덱은 렌더되지
않습니다.

## `@` 참조 문법

| 문법 | 슬라이드에서 이렇게 됩니다 |
|------|---------------------------|
| `@sub_401000` / `@main` / `@0x401000` | 클릭하면 디스어셈블리 뷰로 점프하는 링크 |
| `@main:12` | 클릭하면 의사코드 12번 라인으로 여는 링크 |
| `@main[1:8]` | 디컴파일된 1~8라인을 코드 블록으로 삽입 |
| `@main[7]` | 의사코드 7번 라인만 |
| `@main[]` | 함수 전체 디컴파일 결과 |
| `@main[1:8@5]` | 1~8라인 삽입 + 5번 라인에 `►` 표시 |

`@` 링크에 마우스를 올리면 슬라이드를 벗어나지 않고 디컴파일 코드를
툴팁으로 미리 볼 수 있습니다 (앞 몇 줄, `:라인`이 있으면 그 줄에 `►` 표시) —
발표 중 "이 함수가 뭐였더라?" 확인에 유용합니다.

라인 점프(`:N`)와 임베드(`[a:b]`)는 IDB에서 실시간으로 읽으므로, 리네임하거나
재분석하면 다음 저장 시 반영됩니다. 존재하지 않는 이름은 클릭 시 IDA 출력
창에 알려줍니다.

덱을 열거나 저장할 때마다 덱 전체를 현재 IDB에 대해 검사합니다: 해석되지
않는 `@참조`가 있으면(함수 리네임, 다른 IDB가 열린 경우 등) 툴바 상태에
`⚠ N unresolved @ref(s)`가 뜨고, 마우스를 올리면 목록이, Output 창에는
상세가 표시됩니다 — 발표 중이 아니라 발표 전에 깨진 참조를 잡을 수 있습니다.

반대 방향도 됩니다: 디스어셈블리·의사코드·헥스 뷰에서 우클릭 → **Copy
@reference** — 그 위치의 토큰이 클립보드에 복사되어 덱에 바로 붙여넣을 수
있습니다. 의사코드에서 여러 줄을 드래그하면 그 범위를 임베드 토큰
`@이름[lo:hi]`로 잡고, 아니면 `@이름:라인`(의사코드)·`@이름`을 복사합니다.
이름이 없는 주소나 `@` 토큰 문법으로 표현할 수 없는 이름(Objective-C
셀렉터 등)은 붙여넣어도 항상 동작하도록 `@0x주소`로 복사합니다.

점프해도 키보드 포커스는 덱에 그대로 남으므로,
다시 클릭할 필요 없이 방향키로 계속 슬라이드를 넘길 수 있습니다.

## 덱 작성

각 엔진의 표준 규칙이 그대로 적용됩니다 (front matter, `---` 구분자, 테마,
레이아웃). `@이름` 링크화는 렌더링된 DOM 위에서 동작하며 — Slidev의 동적
마운트 슬라이드는 MutationObserver가 계속 커버합니다 — 본문과 인라인 코드
어디서든 작동합니다. `examples/sample-marp.md`와 `examples/sample-slidev.md`를
참고하세요.

덱은 엔진에 넘어가기 전에 숨김 파일 `.<이름>.ida-slides.md`로 전처리되며
(`[a:b]` 임베드 확장), Marp는 추가로 `.<이름>.ida-slides.html`을 생성합니다.
둘 다 상대 경로 이미지가 깨지지 않도록 `.md` 옆에 두며, 덱을 닫으면 삭제됩니다.
Slidev 개발 서버는 덱을 닫거나 바꾸면 종료됩니다.

## 설치

이 디렉토리를 IDA 플러그인 폴더에 심링크하거나 복사하세요:

```sh
# macOS
ln -s "$(pwd)" ~/.idapro/plugins/ida-slides
```

```powershell
# Windows
New-Item -ItemType Junction -Path "$env:APPDATA\Hex-Rays\IDA Pro\plugins\ida-slides" -Target (Get-Location)
```

IDA 9.2+ (GUI) 필요.

## 테스트

플러그인이 IDA/Qt/네이티브 웹뷰에 묶여 있어 테스트는 `pytest`가 아니라 IDA
안에서 실행합니다. 아무 IDB나 연 뒤 IDA Python 콘솔에서:

```python
exec(open("<repo>/tests/test_in_ida.py", encoding="utf-8").read())
```

순수 로직 검사(토큰 문법, 슬라이드 분할, front matter 파싱, 임베드·린트
처리)는 항상 실행되고, DB 의존 검사(이름 해석, 디컴파일, 라이브 린트)는
열려 있는 IDB에서 함수를 하나 골라 실행하며 IDB가 없으면 건너뜁니다.

Windows에서는 렌더러 자체를 IDA **밖에서** 추가로 테스트할 수 있습니다
(COM 레이어와 marp 파이프라인은 IDB가 필요 없습니다):

```powershell
python tests\test_webview2_standalone.py
```

`webview2_com.py`의 vtable/COM 코드를 건드리기 전에 반드시 이걸 먼저
돌리세요 — 슬롯 인덱스가 틀리면 IDA 안에서 크래시로 나타나는 대신 여기서
깔끔한 실패로 잡힙니다. ida-slides가 열린 IDA가 떠 있어도 안전하게 실행됩니다.

## 구현 노트 (IDA 9.3)

- `PluginForm.FormToPySideWidget`은 `__main__`에 `QtGui`가 있어야 하며, 없으면
  AttributeError가 *조용히 삼켜집니다*. 이 플러그인은 어떤 컨텍스트에서도
  동작하는 `FormToPyQtWidget`(shiboken `wrapInstance`)을 사용합니다.
- WebKit completion handler 블록은 PyObjC 델리게이트 메서드에서 호출할 수
  없고("cannot call block without a signature"), decision handler가 호출되지
  않으면 WebKit이 호스트 프로세스를 abort시킵니다 — 그래서 클릭 라우팅은
  `decidePolicyForNavigationAction` 대신 `WKScriptMessageHandler` +
  `WKUserScript` 클릭 인터셉터를 사용합니다. 블록을 받는 델리게이트 메서드는
  하나도 구현하지 않습니다.
- ObjC 콜백에서 시작되는 IDA API 작업은 전부 `QTimer.singleShot(0, …)`으로
  지연 실행합니다.
- Windows에서는 WebView2를 표준 라이브러리 ctypes만으로 COM 직접 구동합니다
  (`webview2_com.py`) — pip 의존성이 없고 IDA에 번들된 Qt ABI를 전혀 건드리지
  않습니다. 인터페이스 IID와 vtable 슬롯 인덱스는 공식 SDK 헤더에서 가져온
  고정 ABI입니다. COM 콜백에도 동일한 지연 실행 규칙이 적용됩니다.
