#!/bin/zsh
# Upload _assets/, which build_site.py writes, to the asset host over FTP.
#
# Usage: ./upload_assets.sh [--dry-run]
#
# _assets/ holds a directory per release with its download files, which the site links. The
# pages of the site and their data are on GitHub Pages. The asset host serves the directory as
# DOWNLOAD_HOST_URL of build_site.py, https://assets.empirical-software.engineering/data/agenttorrent,
# which is the same directory as REMOTE_DIR below. The script uploads new files and files that are newer locally or differ in
# size, each through a temporary file that replaces the old one when it is complete, and it never
# deletes anything on the server. --dry-run lists the transfers.
#
# The host, the port and the login are read from ~/.config/se-uhd/assets-ftp.conf (or from the
# file in ASSETS_FTP_CONFIG), the file that scripts/sync-assets.sh of se-uhd.github.io reads, as
# KEY=VALUE lines, taken literally. Its FTP_PATH and LOCAL_DIR name the teaching assets and are not
# read here. Requires lftp, which Homebrew packages as the formula lftp.

# Reset the shell options that ~/.zshenv may have changed.
emulate -L zsh
set -euo pipefail

die() { print -ru2 -- "$*"; exit 1; }

REMOTE_DIR=/public_html/assets.sbaltes.com/data/agenttorrent
local_dir=${0:A:h}/_assets

dry_run=""
for arg in "$@"; do
  case $arg in
    --dry-run) dry_run=--dry-run ;;
    -h|--help) sed -n '2,/^$/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $arg" ;;
  esac
done
[[ -n $(print -rl -- $local_dir/*(N/)) ]] || die "$local_dir has no directory of a release. Run build_site.py first."

config=${ASSETS_FTP_CONFIG:-$HOME/.config/se-uhd/assets-ftp.conf}
[[ -f $config ]] || die "Missing $config"
zmodload -F zsh/stat b:zstat
zstat -A st +mode "$config"
if (( st[1] & 077 )); then
  print -ru2 -- "Warning: other users can access $config; run chmod 600 on it"
fi
# Read the values literally, because sourcing the file would expand $ and ` in the password.
# Trailing whitespace, including a CR, is dropped, and then one pair of enclosing quotes is
# removed.
typeset -A cfg
while IFS= read -r line || [[ -n $line ]]; do
  [[ $line =~ '^([A-Z_]+)=((.*[^[:space:]])?)[[:space:]]*$' ]] || continue
  key=$match[1] value=$match[2]
  if [[ $value == \"*\" || $value == \'*\' ]]; then value=${value[2,-2]}; fi
  cfg[$key]=$value
done < "$config"
for key in FTP_HOST FTP_PORT FTP_USER FTP_PASSWORD; do
  [[ -n ${cfg[$key]:-} ]] || die "$config does not set $key"
done
command -v lftp >/dev/null || die "lftp is missing, which Homebrew packages as the formula lftp"

# Quote a value for an lftp command line.
q() { local s=${1//\\/\\\\}; print -rn -- "\"${s//\"/\\\"}\""; }

print -r -- "Uploading $local_dir to ${cfg[FTP_HOST]}:$REMOTE_DIR${dry_run:+ (dry run)}"
# The commands go to lftp on stdin, so the password does not appear in the process list. --norc
# keeps the user's lftp rc files from changing the settings below. lftp prints URLs with the
# password in them (e.g., in a dry run), so sed masks it. The connection requires TLS with a
# valid certificate for FTP_HOST. The cd checks the login and the directory above REMOTE_DIR,
# and mirror creates REMOTE_DIR on the first upload. --parallel transfers several files at once.
lftp --norc <<EOF 2>&1 | LC_ALL=C sed -E 's#(ftp://[^:/@]*):[^@/]*@#\1:***@#g'
set cmd:fail-exit true
set ftp:ssl-force true
set ftp:ssl-protect-data true
set ssl:verify-certificate true
set net:max-retries 2
set xfer:use-temp-file true
open --user $(q "${cfg[FTP_USER]}") --password $(q "${cfg[FTP_PASSWORD]}") -p $(q "${cfg[FTP_PORT]}") $(q "ftp://${cfg[FTP_HOST]}")
cd $(q "${REMOTE_DIR:h}")
mirror --reverse --no-perms --dereference --parallel=8 $dry_run --exclude-glob .DS_Store --exclude-glob '._*' $(q "$local_dir") $(q "$REMOTE_DIR")
EOF
