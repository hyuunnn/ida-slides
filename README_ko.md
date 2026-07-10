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

1. `Ctrl+Shift+M` (또는 View → Open subviews → Marp Presenter: Open Slides…)
2. 마크다운 덱(`.md`) 선택

macOS에서는 덱이 IDA 탭에 임베드된 네이티브 WKWebView로 렌더링됩니다 —
QtWebEngine이 필요 없습니다. 엔진은 덱마다 자동 선택됩니다:

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
- pyobjc-framework-WebKit (플러그인 매니저가 자동 설치; 수동:
  `pip install --user pyobjc-framework-WebKit`)

CLI는 PATH, nvm, Homebrew 순으로 탐색합니다. marp-cli로 내보낸 `.html` 파일도
직접 열 수 있습니다.

### 다른 플랫폼에서의 폴백

- QtWebEngine(`pip install PySide6-Addons`)이 임포트 가능하면 marp-cli HTML
  덱을 같은 방식으로 렌더링합니다.
- 둘 다 없으면 `.md` 덱은 내장 QTextBrowser 슬라이드 뷰어로 렌더링됩니다
  (Marp 문법 규칙 지원, 기본 스타일만). `markdown` 패키지가 필요합니다.

## 덱 작성

## `@` 참조 문법

| 문법 | 슬라이드에서 이렇게 됩니다 |
|------|---------------------------|
| `@sub_401000` / `@main` / `@0x401000` | 클릭하면 디스어셈블리 뷰로 점프하는 링크 |
| `@main:12` | 클릭하면 의사코드 12번 라인으로 여는 링크 |
| `@main[1:8]` | 디컴파일된 1~8라인을 코드 블록으로 삽입 |
| `@main[7]` | 의사코드 7번 라인만 |
| `@main[]` | 함수 전체 디컴파일 결과 |
| `@main[1:8@5]` | 1~8라인 삽입 + 5번 라인에 `►` 표시 |
| `@!main:12` | **프레젠터 팔로우**: 이 슬라이드가 표시되는 순간 IDA가 스스로 점프 (클릭도 가능) |
| `@!main[1:8@5]` | 임베드 + `►` 표시 + 슬라이드 진입 시 5번 라인으로 자동 점프 |

프레젠터 팔로우는 토큰 단위(`!`) 옵트인이라, 덱이 IDA를 끌고 갈 지점과
클릭을 기다릴 지점을 발표 흐름에 맞게 정할 수 있습니다. 툴바의 **Follow @!**
토글로 전체를 켜고 끕니다 — 편집 중엔 끄고, 발표 때 켜세요.

라인 점프(`:N`)와 임베드(`[a:b]`)는 IDB에서 실시간으로 읽으므로, 리네임하거나
재분석하면 다음 저장 시 반영됩니다. 존재하지 않는 이름은 클릭 시 IDA 출력
창에 알려줍니다 (내장 폴백 뷰어에서는 흐리게 표시).

반대 방향도 됩니다: 디스어셈블리·의사코드·헥스 뷰에서 우클릭 → **Copy
@reference** — 그 위치의 토큰(`@이름`, 의사코드에서는 `@이름:라인`, 이름
없는 주소는 `@0x주소`)이 클립보드에 복사되어 덱에 바로 붙여넣을 수 있습니다.

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
ln -s "$(pwd)" ~/.idapro/plugins/ida-slides
```

IDA 9.2+ (GUI) 필요.

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
