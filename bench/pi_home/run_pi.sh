#!/bin/bash
# 在评测容器内运行 pi（Node 由挂载提供）
set -e
export PATH=/opt/node/bin:$PATH
exec node /opt/pi/dist/bundle/cli.js "$@"
