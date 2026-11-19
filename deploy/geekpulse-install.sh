#!/usr/bin/env bash
# First install only. Download from the official stable release, then run with sudo bash.
#
# MIT License
#
# Copyright (c) 2026 zhanglx <changlinghsiao520@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG_COUNT
case "${1:-}" in --help|-h) echo 'sudo bash geekpulse-install.sh [--check]  (Linux + systemd + Nginx, new sites only)'; exit 0;; esac
[[ $# == 0 || ( $# == 1 && $1 == --check ) ]] || { echo 'Unknown option. Use --help.' >&2; exit 2; }
[[ $(uname -s) == Linux && $EUID == 0 && -d /run/systemd/system ]] || { echo 'Run with sudo on the Linux/systemd host that serves Sub2API.' >&2; exit 1; }
for cmd in git curl nginx systemctl useradd; do command -v "$cmd" >/dev/null || { echo "Missing: $cmd" >&2; exit 1; }; done
/usr/bin/python3 -I -c 'import ensurepip, sys, venv; sys.exit(sys.version_info < (3,11))' || { echo 'Install Python 3.11+ and its venv package first.' >&2; exit 1; }
repo=zhanglx-kiko/GeekPulse
umask 022
work=$(mktemp -d /tmp/geekpulse-install.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
curl -fsS --proto '=https' --connect-timeout 8 --max-time 30 "https://api.github.com/repos/$repo/releases/latest" -o "$work/release.json" || { echo 'No stable release available, or GitHub is unreachable. Nothing installed.' >&2; exit 1; }
tag=$(/usr/bin/python3 -I - "$work/release.json" <<'PY'
import json, re, sys
with open(sys.argv[1], encoding='utf-8') as f:
    r = json.load(f)
t = r.get('tag_name', '')
if r.get('draft') is not False or r.get('prerelease') is not False or not re.fullmatch(r'v(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}', t):
    sys.exit('Expected an official stable vX.Y.Z release.')
print(t)
PY
)
# Fetch both refs in one operation. Only execute the tagged commit, never the current main checkout.
src="$work/src"
git_cmd=(git --no-replace-objects -c core.hooksPath=/dev/null -c credential.helper= -c protocol.allow=never -c protocol.https.allow=always -c http.followRedirects=false -c fetch.fsckObjects=true -c http.lowSpeedLimit=100 -c http.lowSpeedTime=60)
"${git_cmd[@]}" init --quiet "$src"
"${git_cmd[@]}" -C "$src" fetch --quiet --atomic --no-tags "https://github.com/$repo.git" refs/heads/main:refs/remotes/origin/main "refs/tags/$tag:refs/tags/$tag"
sha=$("${git_cmd[@]}" -C "$src" rev-parse --verify "refs/tags/$tag^{commit}")
"${git_cmd[@]}" -C "$src" merge-base --is-ancestor "$sha" refs/remotes/origin/main || { echo 'Release is not on official main.' >&2; exit 1; }
"${git_cmd[@]}" -C "$src" checkout --quiet --detach "$sha"
cmp -s -- "${BASH_SOURCE[0]}" "$src/deploy/geekpulse-install.sh" || { echo 'Installer differs from this release. Download its installer again.' >&2; exit 1; }
/usr/bin/python3 -I -B "$src/deploy/first_install.py" "$tag" "$sha" "$@"
