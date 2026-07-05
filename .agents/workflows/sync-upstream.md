---
description: Pull and rebase from aaif-goose/goose main branch while protecting local features
---
# Sync Upstream (Rebase) Workflow

This workflow synchronizes the local repository with the upstream `aaif-goose/goose` main branch, rebasing the user's local commits on top of the latest upstream code.

## Prerequisites
- Working directory should be clean (no uncommitted changes).
- Make sure to review `cargo clippy` and `cargo test` after completion.

## Steps to Execute

1. **Verify git status**
   Ensure working tree is clean. If dirty, stash or commit changes first.
   ```bash
   git status
   ```

2. **Setup Upstream Remote**
   Ensure the `upstream` remote points to the `aaif-goose/goose` repository.
   // turbo
   ```bash
   git remote add upstream https://github.com/aaif-goose/goose.git || true
   ```

3. **Fetch Upstream Changes**
   Fetch the latest `main` branch from `upstream`.
   // turbo
   ```bash
   git fetch upstream main
   ```

4. **Start Rebase**
   Rebase the current branch onto `upstream/main`.
   ```bash
   git rebase upstream/main
   ```

5. **Resolve Conflicts (If any)**
   If rebase stops due to conflicts:
   - Identify conflicted files using `git status`.
   - Use `grep_search` to find conflict markers (`<<<<<<<`).
   - Use `view_file` to understand the context of the conflicts.
   - **CRITICAL**: The goal is to resolve conflicts **without impacting local features**. 
     - The `HEAD` section (top part of the conflict) represents the newly fetched code from `upstream/main`.
     - The later section (bottom part) represents the user's local changes being re-applied.
     - You must merge these carefully: preserve the upstream structural/architectural changes while keeping the user's added functionality intact.
   - Use `multi_replace_file_content` or `replace_file_content` to fix conflicts in the file.
   - Stage the resolved file:
     ```bash
     git add <resolved_file>
     ```
   - Continue the rebase:
     ```bash
     git rebase --continue
     ```
   - *Repeat this step until the rebase completes successfully. Do not proceed arbitrarily; if completely unsure, ask the user!*

6. **Verify the Build**
   Once the rebase is complete, ensure the project builds correctly and passes tests as per `AGENTS.md` instructions.
   ```bash
   cargo clippy --all-targets -- -D warnings
   cargo build
   cargo test
   ```
