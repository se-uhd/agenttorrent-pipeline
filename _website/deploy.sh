#!/usr/bin/env bash
set -euo pipefail

# Copies the files that a deployment of the dataset explorer website through GitHub Pages
# needs to a target directory. The target directory should be the root of a GitHub
# repository configured for deployment through GitHub Pages.
#
# Usage: ./deploy.sh <target-directory>

if [ $# -ne 1 ]; then
  echo "Usage: $0 <target-directory>"
  exit 1
fi

TARGET="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SITE_DIR="$SCRIPT_DIR/_site"

if [ ! -d "$SITE_DIR" ]; then
  echo "Error: _site/ directory not found. Run build_site.py first."
  exit 1
fi

if [ ! -f "$SITE_DIR/index.html" ]; then
  echo "Error: _site/index.html not found. Run build_site.py first."
  exit 1
fi

mkdir -p "$TARGET"

# Copy site contents
echo "Copying site to $TARGET..."
rsync -a --delete --exclude='.git' "$SITE_DIR/" "$TARGET/"

# Create .nojekyll to prevent GitHub Pages from processing with Jekyll
touch "$TARGET/.nojekyll"

# Create .gitignore
cat > "$TARGET/.gitignore" << 'EOF'
.DS_Store
*.swp
*~
EOF

# Create GitHub Actions workflow for deployment
WORKFLOW_DIR="$TARGET/.github/workflows"
mkdir -p "$WORKFLOW_DIR"
cat > "$WORKFLOW_DIR/deploy.yml" << 'EOF'
name: Deploy to GitHub Pages

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: .
      - id: deployment
        uses: actions/deploy-pages@v4
EOF

# Count files and total size
FILE_COUNT=$(find "$TARGET" -type f | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$TARGET" | cut -f1)

echo "Copied $FILE_COUNT files ($TOTAL_SIZE) to $TARGET"
echo ""
echo "Next steps:"
echo "  cd $TARGET"
echo "  git init && git add -A && git commit -m 'Initial deployment'"
echo "  git remote add origin <your-repo-url>"
echo "  git push -u origin main"
echo "  # Enable GitHub Pages (Settings > Pages > Source: GitHub Actions)"
