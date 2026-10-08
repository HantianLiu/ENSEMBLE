#!/usr/bin/env bash
# Copy this template to the repository root as publish_github.local.sh.
# The local copy is gitignored: never put a real token in this public template.
set +x
set -euo pipefail
umask 077

GITHUB_USERNAME="HantianLiu"
PASSWORD=""  # GitHub personal access token; fill ONLY the ignored local copy, not this public template.
REPOSITORY_URL="https://github.com/HantianLiu/ENSEMBLE.git"
BRANCH="main"
VERSION="0.7.5"
COMMIT_MESSAGE="Release ENSEMBLE v0.7.5"

fail() { printf '%s\n' "$*" >&2; exit 1; }
dry_run=false
case "${1:-}" in
  "") ;;
  --dry-run) dry_run=true ;;
  *) fail "用法：bash publish_github.local.sh [--dry-run]" ;;
esac
[[ $# -le 1 ]] || fail "参数过多。"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[[ "$(basename -- "${BASH_SOURCE[0]}")" == "publish_github.local.sh" ]] ||
  fail "请复制模板至项目根目录：cp scripts/publish_github.template.sh publish_github.local.sh；只在本地副本填写令牌。"
cd -- "$script_dir"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "脚本必须放在 ENSEMBLE 的 Git 工作目录中。"
[[ "$(git rev-parse --show-toplevel)" == "$script_dir" ]] || fail "脚本必须放在仓库根目录。"
if git ls-files --error-unmatch -- publish_github.local.sh >/dev/null 2>&1; then
  fail "本地认证脚本已被 Git 跟踪；为防泄露，拒绝提交。请先将它移出 Git 跟踪。"
fi
git check-ignore -q publish_github.local.sh || fail "本地认证脚本未被 .gitignore 排除；拒绝提交。"
[[ "$(git symbolic-ref --quiet --short HEAD)" == "$BRANCH" ]] || fail "当前不在 main 分支；请先人工检查分支。"
[[ "$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)" == "$VERSION" ]] ||
  fail "pyproject.toml 版本不是 0.7.5；拒绝打错版本标签。"
if [[ -f scripts/publish_github.template.sh ]]; then
  grep -qx 'PASSWORD=""  # GitHub personal access token; fill ONLY the ignored local copy, not this public template.' scripts/publish_github.template.sh ||
    fail "公开模板中的 PASSWORD 必须保持为空；只填写 publish_github.local.sh。"
fi
git diff --cached --quiet || fail "已有暂存改动；为避免混入其他内容，请先处理暂存区后再运行。"
origin_url="$(git remote get-url origin 2>/dev/null || true)"
[[ -z "$origin_url" || "$origin_url" == "$REPOSITORY_URL" ]] ||
  fail "origin 与预期的 HantianLiu/ENSEMBLE 不一致，请先人工核对；脚本不会修改远端配置。"

# Explicit source scope excludes live configuration, meetings, virtualenvs,
# local authentication, build outputs and release archives.
publish_paths=()
for candidate in src docs tests examples scripts generated_schemas .github \
  .gitignore .gitattributes pyproject.toml MANIFEST.in LICENSE README.md \
  README_v071.md README_v071.en.md VERSION.md CONTRIBUTING.md \
  PROJECT_OVERVIEW_CN.txt api.md AGENTS.md TODO.md THIRD_PARTY_NOTICES.md \
  RELEASE_NOTES_0.7.5.md; do
  if [[ -e "$candidate" ]] || git ls-files --error-unmatch -- "$candidate" >/dev/null 2>&1; then
    publish_paths+=("$candidate")
  fi
done
[[ ${#publish_paths[@]} -gt 0 ]] || fail "未找到可提交的源码。"
tag="v$VERSION"
if git rev-parse --verify "refs/tags/$tag" >/dev/null 2>&1; then
  [[ "$(git rev-parse "$tag^{commit}")" == "$(git rev-parse HEAD)" ]] ||
    fail "本地 v0.7.5 标签指向其他提交；不会覆盖已有标签。"
  [[ -z "$(git status --porcelain --untracked-files=all -- "${publish_paths[@]}")" ]] ||
    fail "本地 v0.7.5 标签已存在，但源码又有变动；请先人工决定版本，不覆盖已发布版本。"
fi
if $dry_run; then
  printf '只读预览：%s → %s；标签 %s\n' "$script_dir" "$REPOSITORY_URL" "$tag"
  git status --short -- "${publish_paths[@]}"
  printf '%s\n' "未暂存、未提交、未打标签、未联网；本地认证脚本和 dist 不会提交。"
  exit 0
fi

PASSWORD="${PASSWORD:-${ENSEMBLE_GITHUB_TOKEN:-}}"
[[ -n "$PASSWORD" ]] || fail "请在本地脚本的 PASSWORD=\"\" 内填写 GitHub 访问令牌，或设置 ENSEMBLE_GITHUB_TOKEN。"
[[ "$PASSWORD" != *$'\n'* && "$PASSWORD" != *$'\r'* ]] || fail "令牌不能含换行符。"
# Never put credentials in a URL, Git config or command-line arguments.
unset GIT_TRACE GIT_TRACE_PACKET GIT_TRACE_CURL GIT_CURL_VERBOSE GIT_TRACE_CURL_NO_DATA
export GIT_ASKPASS_REQUIRE=force GIT_TERMINAL_PROMPT=0
unset ENSEMBLE_GITHUB_TOKEN
publish_tmp="$(mktemp -d -t ensemble-publish-075.XXXXXXXX)"
cleanup() {
  unset ENSEMBLE_GITHUB_PASSWORD PASSWORD
  [[ ! -f "$publish_tmp/askpass.sh" ]] || rm -- "$publish_tmp/askpass.sh"
  rmdir -- "$publish_tmp" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' '#!/usr/bin/env bash' 'set +x' \
  'case "$1" in' \
  '  *"'\''https://github.com'\''"*|*"'\''https://github.com/"*|*"'\''https://HantianLiu@github.com'\''"*|*"'\''https://HantianLiu@github.com/"*) ;;' \
  '  *) exit 1 ;;' 'esac' 'case "$1" in' \
  '  *Username*) printf "%s\n" "$ENSEMBLE_GITHUB_USERNAME" ;;' \
  '  *Password*) printf "%s\n" "$ENSEMBLE_GITHUB_PASSWORD" ;;' \
  '  *) exit 1 ;;' 'esac' > "$publish_tmp/askpass.sh"
chmod 700 "$publish_tmp/askpass.sh"
export GIT_ASKPASS="$publish_tmp/askpass.sh"
git_auth() {
  ENSEMBLE_GITHUB_USERNAME="$GITHUB_USERNAME" ENSEMBLE_GITHUB_PASSWORD="$PASSWORD" LC_ALL=C \
    git -c credential.helper= -c credential.interactive=false \
        -c core.hooksPath=/dev/null -c http.followRedirects=false -c http.extraHeader= "$@"
}
git_identity() {
  git -c user.name="$GITHUB_USERNAME" \
      -c user.email="$GITHUB_USERNAME@users.noreply.github.com" "$@"
}
printf '%s\n' "检查远端 main；不自动合并或覆盖远端历史……"
git_auth fetch --no-tags "$REPOSITORY_URL" "refs/heads/$BRANCH"
git merge-base --is-ancestor FETCH_HEAD HEAD ||
  fail "远端 main 有本地尚未包含的提交；请先人工同步并检查，再重新运行。"
git add -A -- "${publish_paths[@]}"
if ! git diff --cached --quiet; then
  git_identity commit -m "$COMMIT_MESSAGE"
else
  printf '%s\n' "没有新的源码改动；推送当前提交。"
fi
if ! git rev-parse --verify "refs/tags/$tag" >/dev/null 2>&1; then
  git_identity tag -a "$tag" -m "ENSEMBLE v$VERSION"
fi
git_auth push --atomic "$REPOSITORY_URL" \
  "HEAD:refs/heads/$BRANCH" "refs/tags/$tag:refs/tags/$tag" ||
  fail "推送失败；本地提交与标签已保留，未强推。请检查令牌权限和远端状态后重试。"
printf '已提交并推送：%s · %s\n' "$REPOSITORY_URL" "$tag"
