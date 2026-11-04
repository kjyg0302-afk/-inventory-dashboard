# Hugging Face Spaces(Docker)용. Streamlit Community Cloud는 이 파일을 쓰지 않는다.
# 2026-11-04 Streamlit Cloud에서 동시 요청 일부가 멈추는 장애가 계속돼 옮길 곳으로 준비했다.
FROM python:3.12-slim

# Spaces는 uid 1000 사용자로 실행한다
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH

EXPOSE 7860
CMD ["bash", "hf/start.sh"]
