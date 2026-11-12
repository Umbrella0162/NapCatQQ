# NapCat x86_64 镜像 —— 逐条复刻官方 mlikiowa/napcat-docker 的构建过程
#
# 依据：本机 Nix 闭包里的官方镜像（dockerTools.pullImage 固定输出）
#   /nix/store/x1qn5yfbj034xx9ad68sshf8941vqdn1-docker-image-mlikiowa-napcat-docker-v4.18.41.tar
# 其 image config 的 history 给出了每一条构建指令；本文件的每条 RUN 都与之对应，
# 差异只在构建期注入的文件（NapCat.Shell.zip 由本仓库源码构建而非取自 release）。
#
# 指纹基线见 docker/official-contract.json，校验脚本 scripts/verify-parity.sh。

FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

# 与官方同一份 apt 清单（顺序亦照抄），末尾的时区/清理命令同样保留。
# hadolint 的 DL3008/DL3015/DL3027 在这里是有意违反的：官方镜像就是不锁 apt 版本、
# 装 recommends、用 apt 而非 apt-get install 的语义，改动会偏离行为基线。
# hadolint ignore=DL3008,DL3015,DL3027
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnotify4 \
    libsecret-1-0 \
    libgbm1 \
    xvfb \
    libasound2 \
    fonts-wqy-zenhei \
    gnutls-bin \
    libglib2.0-dev \
    libdbus-1-3 \
    libgtk-3-0 \
    libxss1 \
    libxtst6 \
    libatspi2.0-0 \
    libx11-xcb1 \
    ffmpeg \
    unzip \
    dbus-user-session \
    curl \
    jq \
    gosu \
    tzdata && \
    echo "${TZ}" > /etc/timezone && \
    ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    apt autoremove -y && \
    apt clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 官方用的是默认 uid/gid 1000 的 napcat 用户，/app 为家目录
RUN useradd --no-log-init -d /app napcat

WORKDIR /app

# NapCat.Shell.zip：由本仓库源码在 CI 中构建（见 .github/workflows/docker.yml）
# entrypoint.sh / templates：与官方镜像内的文件逐字节一致
COPY NapCat.Shell.zip entrypoint.sh /app/
COPY templates/ /app/templates/

# QQ 本体：官方从 qqdl.gtimg.cn 取对应架构的 deb，dpkg 强装
# QQ_ARCH 由 CI 按 linux/amd64 传入（官方此处是 arch | sed 自动换算）
ARG QQ_VERSION_PATH=9.9.36/beta/9ee04bef/linuxqq_3.2.34-53644
ARG QQ_ARCH=amd64
RUN curl --retry 3 --connect-timeout 30 --max-time 600 -fL -o linuxqq.deb \
        "https://qqdl.gtimg.cn/qqfile/QQNT/${QQ_VERSION_PATH}_${QQ_ARCH}.deb" && \
    dpkg -i --force-depends linuxqq.deb && rm linuxqq.deb

# 把 NapCat 挂进 QQ 的 Electron 主进程
RUN chmod +x entrypoint.sh && \
    echo "(async () => {await import('file:///app/napcat/napcat.mjs');})();" > /opt/QQ/resources/app/loadNapCat.js && \
    sed -i 's|"main": "[^"]*"|"main": "./loadNapCat.js"|' /opt/QQ/resources/app/package.json

VOLUME ["/app/napcat/config"]
VOLUME ["/app/.config/QQ"]

ENTRYPOINT ["bash", "entrypoint.sh"]