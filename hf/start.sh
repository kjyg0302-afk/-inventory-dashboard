#!/bin/bash
# Hugging Face Space 시작 스크립트. Space의 Secrets는 환경변수로만 들어오므로,
# secrets.toml 내용 전체를 STREAMLIT_SECRETS 하나에 넣어 두고 여기서 파일로 풀어 쓴다.
set -e
mkdir -p "$HOME/.streamlit"
if [ -n "$STREAMLIT_SECRETS" ]; then
  printf '%s\n' "$STREAMLIT_SECRETS" > "$HOME/.streamlit/secrets.toml"
fi
# Spaces 페이지는 앱을 iframe으로 감싸서 XSRF 쿠키가 막히면 파일 업로드가 403이 난다 → 끈다 (앱 자체 로그인은 그대로).
exec streamlit run app.py \
  --server.port 7860 --server.address 0.0.0.0 --server.headless true \
  --server.enableXsrfProtection false --browser.gatherUsageStats false
