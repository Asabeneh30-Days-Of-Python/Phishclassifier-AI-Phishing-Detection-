#!/bin/sh
ls -l /usr/bin/env /bin/env /usr/bin/bash /bin/bash /bin/sh || true
which env || true
which bash || true
echo "SHELL=$SHELL"