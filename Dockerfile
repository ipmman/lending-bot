# Use the official Python 3.11 slim image; the tag follows the latest 3.11 patch release.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ="Asia/Taipei" \
    UV_INSTALL_DIR="/usr/local/bin" \
    UV_SYSTEM_PYTHON=1

RUN apt-get update && \
    # ca-certificates is listed so the build upgrades it to the newest CA bundle.
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    nano && \
    # Install uv using the official installer.
    # See: https://github.com/astral-sh/uv
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    uv --version && \
    # Clean apt cache to reduce image size.
    rm -rf /var/lib/apt/lists/*


# Set the working directory.
WORKDIR /workspace

# Copy requirements first so Docker can reuse the dependency layer when it is unchanged.
COPY requirements.txt /workspace/requirements.txt

# The image's own pip carries its own CA bundle; 26.2.2 is the first release
# without the compromised emSign root, so upgrade it alongside the requirements.
RUN uv pip install --system --no-cache "pip>=26.2.2" -r requirements.txt

RUN  mkdir -p /workspace/logs
# Copy the application code into the image.
COPY ./code/ /workspace/code/

# Optional: uncomment and adjust if the application exposes a network port.
# EXPOSE 8000

# Run the bot by default.
CMD ["python", "/workspace/code/main.py"]
