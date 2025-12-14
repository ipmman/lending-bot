# 使用官方 Python 3.11.5 作為基礎映像檔
FROM python:3.11.5-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ="Asia/Taipei" \
    UV_INSTALL_DIR="/usr/local/bin" \
    UV_SYSTEM_PYTHON=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    nano && \
    # 使用官方腳本安裝 uv
    # 詳細資訊: https://github.com/astral-sh/uv
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    uv --version && \
    # 清理 apt 快取以減少映像檔大小
    rm -rf /var/lib/apt/lists/*


# 設定工作目錄
WORKDIR /workspace

# 複製 requirements.txt 到工作目錄
# 這樣做的好處是，如果 requirements.txt 沒有改變，Docker 可以利用快取，加速建置過程
COPY requirements.txt /workspace/requirements.txt

RUN uv pip install --system --no-cache -r requirements.txt

RUN  mkdir -p /workspace/logs
# 複製專案的其餘程式碼到工作目錄
COPY ./code/ /workspace/code/

# (可選) 如果你的應用程式是網路應用，取消註解並修改以下這行來暴露連接埠
# EXPOSE 8000

# 設定容器啟動時執行的命令
# 假設你的主程式檔案是 main.py
CMD ["python", "/workspace/code/main.py"]
