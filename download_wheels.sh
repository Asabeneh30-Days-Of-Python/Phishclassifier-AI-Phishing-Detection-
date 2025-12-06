#!/bin/sh
set -euo pipefail
pip install --upgrade pip wheel setuptools
while IFS= read -r pkg || [ -n "$pkg" ]; do
  echo "Downloading $pkg"
  pip download --dest /wheels --only-binary=:all: --no-deps "$pkg" || pip download --dest /wheels --no-deps "$pkg"
done < /packages.txt