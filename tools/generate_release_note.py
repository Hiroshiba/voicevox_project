"""VOICEVOX関連リポジトリのリリースノートを生成します。

例:
python tools/generate_release_note.py editor --version 0.25.1 --summary "* １行目" --summary "* ２行目"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote


def main() -> None:
    """リリースノート生成コマンドを実行します。"""
    try:
        options = build_options(parse_args())
        body = generate_release_note(options)
        write_body(body, options.output_path)
    except ReleaseNoteError as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1) from error


TargetKey = Literal["editor", "engine", "core", "resource", "vvm"]
SummaryPolicy = Literal["required", "forbidden"]
AssetMissingPolicy = Literal["required", "strikethrough"]
EnginePackageKind = Literal["7z", "vvpp"]


@dataclass(frozen=True)
class ReleaseNoteError(Exception):
    """リリースノート生成の失敗を表します。"""

    message: str

    def __str__(self) -> str:
        """エラーメッセージを返します。"""
        return self.message


@dataclass(frozen=True)
class TargetConfig:
    """対象リポジトリの設定を表します。"""

    key: TargetKey
    repository: str
    summary_policy: SummaryPolicy


@dataclass(frozen=True)
class Options:
    """コマンドラインオプションを表します。"""

    output_path: Path | None
    previous_version: str | None
    ref: str
    summary_text: str | None
    target: TargetConfig
    version: str | None


@dataclass(frozen=True)
class ReleaseSummary:
    """GitHub Releaseの概要を表します。"""

    is_draft: bool
    published_at: datetime
    tag_name: str


@dataclass(frozen=True)
class Asset:
    """GitHub Releaseのアセットを表します。"""

    name: str
    url: str


@dataclass(frozen=True)
class AssetExpectations:
    """想定するGitHub Releaseアセット名を表します。"""

    allowed_names: set[str]
    required_names: set[str]


@dataclass(frozen=True)
class AssetLinkSpec:
    """アセットリンクの生成設定を表します。"""

    asset_name: str
    label: str
    missing_policy: AssetMissingPolicy


@dataclass(frozen=True)
class AssetTextSpec:
    """固定テキストの配布物項目を表します。"""

    text: str


@dataclass(frozen=True)
class EditorAssetSection:
    """エディタの配布物セクションを表します。"""

    items: tuple[AssetLinkSpec | AssetTextSpec, ...]
    title: str


@dataclass(frozen=True)
class EnginePackageSpec:
    """エンジンの配布物設定を表します。"""

    label: str
    minimum_7z_part_count: int
    minimum_vvpp_part_count: int | None
    platform: str


@dataclass(frozen=True)
class CoreKeyChanges:
    """VOICEVOX COREの主要変更点を表します。"""

    change_text: str
    reference_text: str
    thanks_text: str


TARGET_CONFIGS: dict[TargetKey, TargetConfig] = {
    "editor": TargetConfig(
        key="editor",
        repository="VOICEVOX/voicevox",
        summary_policy="required",
    ),
    "engine": TargetConfig(
        key="engine",
        repository="VOICEVOX/voicevox_engine",
        summary_policy="required",
    ),
    "core": TargetConfig(
        key="core",
        repository="VOICEVOX/voicevox_core",
        summary_policy="forbidden",
    ),
    "resource": TargetConfig(
        key="resource",
        repository="VOICEVOX/voicevox_resource",
        summary_policy="forbidden",
    ),
    "vvm": TargetConfig(
        key="vvm",
        repository="VOICEVOX/voicevox_vvm",
        summary_policy="required",
    ),
}

TARGET_ALIASES: dict[str, TargetKey] = {
    "core": "core",
    "editor": "editor",
    "engine": "engine",
    "resource": "resource",
    "vvm": "vvm",
    "voicevox": "editor",
    "voicevox_core": "core",
    "voicevox_editor": "editor",
    "voicevox_engine": "engine",
    "voicevox_resource": "resource",
    "voicevox_vvm": "vvm",
}

DEFAULT_CORE_REF = "main"
CORE_ASSET_TABLE_HEADERS = ("OS", "ｱｰｷﾃｸﾁｬ", "名前")
CORE_DOWNLOADER_ASSET_ROWS = (
    ("Windows", "x64", "download-windows-x64.exe"),
    ("macOS", "x64", "download-osx-x64"),
    ("macOS", "arm64", "download-osx-arm64"),
    ("Linux", "x64", "download-linux-x64"),
    ("Linux", "arm64", "download-linux-arm64"),
)

ENGINE_PACKAGE_SPECS = (
    EnginePackageSpec(
        label="Windows（CPU版）",
        minimum_7z_part_count=1,
        minimum_vvpp_part_count=None,
        platform="windows-cpu",
    ),
    EnginePackageSpec(
        label="Windows（GPU/DirectML版）",
        minimum_7z_part_count=1,
        minimum_vvpp_part_count=None,
        platform="windows-directml",
    ),
    EnginePackageSpec(
        label="Windows（GPU/CUDA版）",
        minimum_7z_part_count=2,
        minimum_vvpp_part_count=2,
        platform="windows-nvidia",
    ),
    EnginePackageSpec(
        label="macOS（CPU/x64版）",
        minimum_7z_part_count=1,
        minimum_vvpp_part_count=None,
        platform="macos-x64",
    ),
    EnginePackageSpec(
        label="macOS（CPU/arm64版）",
        minimum_7z_part_count=1,
        minimum_vvpp_part_count=None,
        platform="macos-arm64",
    ),
    EnginePackageSpec(
        label="Linux（CPU/x64版）",
        minimum_7z_part_count=1,
        minimum_vvpp_part_count=None,
        platform="linux-cpu-x64",
    ),
    EnginePackageSpec(
        label="Linux（CPU/arm64版）",
        minimum_7z_part_count=1,
        minimum_vvpp_part_count=None,
        platform="linux-cpu-arm64",
    ),
    EnginePackageSpec(
        label="Linux（GPU/CUDA版）",
        minimum_7z_part_count=2,
        minimum_vvpp_part_count=2,
        platform="linux-nvidia",
    ),
)


def parse_args() -> argparse.Namespace:
    """コマンドライン引数を解析します。"""
    parser = argparse.ArgumentParser(
        description="VOICEVOX関連リポジトリのリリースノートを生成します。",
    )
    parser.add_argument(
        "target",
        choices=sorted(TARGET_ALIASES.keys()),
        help="生成対象です。",
    )
    parser.add_argument(
        "--version",
        help="対象のバージョンです。省略時は公開済みリリースから最新を選びます。",
    )
    parser.add_argument(
        "--previous-version",
        help="GitHubの自動生成リリースノートで比較元に使うバージョンです。",
    )
    parser.add_argument(
        "--summary",
        action="append",
        help="手入力の更新内容です。Markdownとしてそのまま使います。複数回指定できます。",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="手入力の更新内容を読むMarkdownファイルです。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="生成した本文を書き込むファイルです。省略時は標準出力に出します。",
    )
    parser.add_argument(
        "--ref",
        default=DEFAULT_CORE_REF,
        help="coreのkey-changes取得に使うGit参照です。",
    )
    return parser.parse_args()


def build_options(args: argparse.Namespace) -> Options:
    """解析済み引数から実行オプションを組み立てます。"""
    raw_target = expect_namespace_str(args, "target")
    target_key = TARGET_ALIASES[raw_target]
    target = TARGET_CONFIGS[target_key]
    summary_text = read_summary_text(args)

    if target.summary_policy == "required" and summary_text is None:
        raise ReleaseNoteError(
            f"{raw_target} には --summary または --summary-file が必要です"
        )

    if target.summary_policy == "forbidden" and summary_text is not None:
        raise ReleaseNoteError(
            f"{raw_target} では --summary と --summary-file は使いません"
        )

    return Options(
        output_path=expect_namespace_optional_path(args, "output"),
        previous_version=expect_namespace_optional_str(args, "previous_version"),
        ref=expect_namespace_str(args, "ref"),
        summary_text=summary_text,
        target=target,
        version=expect_namespace_optional_str(args, "version"),
    )


def read_summary_text(args: argparse.Namespace) -> str | None:
    """手入力の更新内容を読み込みます。"""
    summary_values = expect_namespace_optional_str_list(args, "summary")
    summary_file = expect_namespace_optional_path(args, "summary_file")

    if summary_values is not None and summary_file is not None:
        raise ReleaseNoteError("--summary と --summary-file は同時に指定できません")

    if summary_values is not None:
        summary_text = "\n".join(summary_values)
        return validate_summary_text(summary_text)

    if summary_file is not None:
        try:
            summary_text = summary_file.read_text(encoding="utf-8")
        except OSError as error:
            raise ReleaseNoteError(
                f"summaryファイルを読めませんでした: {summary_file}"
            ) from error
        return validate_summary_text(summary_text)

    return None


def validate_summary_text(summary_text: str) -> str:
    """手入力の更新内容が空でないことを確認します。"""
    normalized_summary_text = normalize_newlines(summary_text)
    if len(normalized_summary_text.strip()) == 0:
        raise ReleaseNoteError("更新内容が空です")
    return normalized_summary_text


def generate_release_note(options: Options) -> str:
    """対象に応じたリリースノートを生成します。"""
    version = resolve_version(options)

    if options.target.key == "editor":
        body = generate_editor_release_note(options, version)
    elif options.target.key == "engine":
        body = generate_engine_release_note(options, version)
    elif options.target.key == "core":
        body = generate_core_release_note(options, version)
    elif options.target.key == "resource":
        body = generate_resource_release_note(options, version)
    elif options.target.key == "vvm":
        body = generate_vvm_release_note(options, version)
    else:
        raise ReleaseNoteError(f"未対応の対象です: {options.target.key}")

    return body


def resolve_version(options: Options) -> str:
    """指定バージョンまたは最新公開リリースのバージョンを返します。"""
    if options.version is not None:
        return options.version

    releases = list_releases(options.target.repository)
    published_releases = [release for release in releases if release.is_draft is False]
    if len(published_releases) == 0:
        raise ReleaseNoteError(
            f"{options.target.repository} に公開済みリリースがありません"
        )

    latest_release = max(published_releases, key=lambda release: release.published_at)
    return latest_release.tag_name


def generate_editor_release_note(options: Options, version: str) -> str:
    """エディタのリリースノートを生成します。"""
    assets = get_release_assets(options.target.repository, version)
    validate_expected_assets(
        assets,
        collect_editor_asset_expectations(version, assets),
        "エディタ",
    )
    generated_notes = generate_notes_body(
        options.target.repository,
        version,
        options.previous_version,
    )
    summary_text = expect_summary_text(options)

    lines: list[str] = []
    lines.extend(build_editor_asset_lines(options.target.repository, version, assets))
    append_blank_line(lines)
    lines.append("## 追加機能")
    append_blank_line(lines)
    append_markdown_block(lines, summary_text)
    append_optional_generated_notes(lines, generated_notes)
    return render_lines(lines)


def generate_engine_release_note(options: Options, version: str) -> str:
    """エンジンのリリースノートを生成します。"""
    assets = get_release_assets(options.target.repository, version)
    validate_expected_assets(
        assets,
        collect_engine_asset_expectations(version, assets),
        "エンジン",
    )
    generated_notes = generate_notes_body(
        options.target.repository,
        version,
        options.previous_version,
    )
    summary_text = expect_summary_text(options)

    lines: list[str] = []
    append_markdown_block(lines, summary_text)
    append_blank_line(lines)
    lines.append("## パッケージ案内")
    append_blank_line(lines)
    lines.extend(build_engine_asset_lines(options.target.repository, version, assets))
    append_optional_generated_notes(lines, generated_notes)
    return render_lines(lines)


def generate_core_release_note(options: Options, version: str) -> str:
    """コアのリリースノートを生成します。"""
    assets = get_release_assets(options.target.repository, version)
    validate_expected_assets(
        assets,
        collect_core_asset_expectations(version),
        "コア",
    )
    key_changes = fetch_core_key_changes(version, options.ref)

    lines = [
        "これは VOICEVOX の音声合成コアです。詳細は [ユーザーガイド](https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/usage.md) をご覧ください。",
        "",
        "> [!NOTE]",
        "> 音声モデル（VVM ファイル）には利用規約が存在します。詳細は [ユーザーガイド](https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/usage.md) をご覧ください。",
        "",
        "## 変更点",
        "",
        f"<details><summary>{version}の主要な変更点</summary>",
        "",
    ]
    append_markdown_block(lines, key_changes.change_text)
    lines.append("</details>")
    append_blank_line(lines)
    lines.append("## アセット一覧")
    append_blank_line(lines)
    lines.extend(build_core_asset_lines(options.target.repository, version, assets))

    if len(key_changes.thanks_text) > 0:
        append_blank_line(lines)
        lines.append("## 謝辞")
        append_blank_line(lines)
        append_markdown_block(lines, key_changes.thanks_text)

    if len(key_changes.reference_text) > 0:
        append_blank_line(lines)
        append_markdown_block(lines, key_changes.reference_text)

    return render_lines(lines)


def generate_resource_release_note(options: Options, version: str) -> str:
    """リソースのリリースノートを生成します。"""
    generated_notes = generate_notes_body(
        options.target.repository,
        version,
        options.previous_version,
    )
    lines: list[str] = []
    append_markdown_block(lines, generated_notes)
    return render_lines(lines)


def generate_vvm_release_note(options: Options, version: str) -> str:
    """VVMのリリースノートを生成します。"""
    generated_notes = generate_notes_body(
        options.target.repository,
        version,
        options.previous_version,
    )
    summary_text = expect_summary_text(options)

    lines = [
        "## 内容",
        "",
    ]
    append_markdown_block(lines, summary_text)
    append_blank_line(lines)
    lines.append(
        "詳細や利用規約は [README](https://github.com/VOICEVOX/voicevox_vvm/blob/main/README.md) をご参照ください。"
    )
    append_optional_generated_notes(lines, generated_notes)
    return render_lines(lines)


def expect_summary_text(options: Options) -> str:
    """必須の更新内容を返します。"""
    if options.summary_text is None:
        raise ReleaseNoteError(f"{options.target.key} には更新内容が必要です")
    return options.summary_text


def list_releases(repository: str) -> list[ReleaseSummary]:
    """GitHub Releaseの一覧を取得します。"""
    response = run_gh_json(
        [
            "gh",
            "release",
            "list",
            "-R",
            repository,
            "--limit",
            "100",
            "--json",
            "tagName,isDraft,publishedAt",
        ]
    )
    if not isinstance(response, list):
        raise ReleaseNoteError("リリース一覧の形式が不正です")

    releases: list[ReleaseSummary] = []
    for index, item in enumerate(response):
        if not isinstance(item, dict):
            raise ReleaseNoteError(f"リリース一覧の {index} 番目の形式が不正です")
        releases.append(parse_release_summary(item, index))
    return releases


def parse_release_summary(item: dict[object, object], index: int) -> ReleaseSummary:
    """GitHub Release一覧の1項目を変換します。"""
    tag_name = expect_mapping_str(item, "tagName", f"リリース一覧の {index} 番目")
    is_draft = expect_mapping_bool(item, "isDraft", f"リリース一覧の {index} 番目")
    published_at_text = expect_mapping_str(
        item,
        "publishedAt",
        f"リリース一覧の {index} 番目",
    )
    try:
        published_at = datetime.fromisoformat(published_at_text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseNoteError(
            f"リリース一覧の {index} 番目の公開日時が不正です: {published_at_text}"
        ) from error
    return ReleaseSummary(
        is_draft=is_draft,
        published_at=published_at,
        tag_name=tag_name,
    )


def get_release_assets(repository: str, version: str) -> dict[str, Asset]:
    """GitHub Releaseのアセット一覧を取得します。"""
    response = run_gh_json(
        [
            "gh",
            "release",
            "view",
            "-R",
            repository,
            version,
            "--json",
            "assets",
        ]
    )
    if not isinstance(response, dict):
        raise ReleaseNoteError("リリース情報の形式が不正です")

    raw_assets = expect_mapping_list(response, "assets", "リリース情報")
    assets: dict[str, Asset] = {}
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, dict):
            raise ReleaseNoteError(f"アセット一覧の {index} 番目の形式が不正です")
        asset = parse_asset(raw_asset, index)
        assets[asset.name] = asset
    return assets


def parse_asset(item: dict[object, object], index: int) -> Asset:
    """GitHub Releaseアセットを変換します。"""
    return Asset(
        name=expect_mapping_str(item, "name", f"アセット一覧の {index} 番目"),
        url=expect_mapping_str(item, "url", f"アセット一覧の {index} 番目"),
    )


def generate_notes_body(
    repository: str,
    version: str,
    previous_version: str | None,
) -> str:
    """GitHubの自動生成リリースノート本文を取得します。"""
    command = [
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{repository}/releases/generate-notes",
        "-f",
        f"tag_name={version}",
    ]
    if previous_version is not None:
        command.extend(["-f", f"previous_tag_name={previous_version}"])

    response = run_gh_json(command)
    if not isinstance(response, dict):
        raise ReleaseNoteError("自動生成リリースノートの形式が不正です")

    return normalize_newlines(
        expect_mapping_str(response, "body", "自動生成リリースノート")
    )


def fetch_core_key_changes(version: str, ref: str) -> CoreKeyChanges:
    """VOICEVOX COREの主要変更点をリモートから取得します。"""
    path = f"docs/guide/user/key-changes/{version}.md"
    encoded_ref = quote(ref, safe="")
    response = run_gh_json(
        [
            "gh",
            "api",
            f"repos/VOICEVOX/voicevox_core/contents/{path}?ref={encoded_ref}",
        ]
    )
    if not isinstance(response, dict):
        raise ReleaseNoteError("coreのkey-changesの形式が不正です")

    content = expect_mapping_str(response, "content", "coreのkey-changes")
    encoding = expect_mapping_str(response, "encoding", "coreのkey-changes")
    if encoding != "base64":
        raise ReleaseNoteError(f"coreのkey-changesのエンコードが不正です: {encoding}")

    try:
        decoded_text = base64.b64decode(content).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseNoteError(
            "coreのkey-changesをUTF-8として読めませんでした"
        ) from error
    except ValueError as error:
        raise ReleaseNoteError(
            "coreのkey-changesをbase64として読めませんでした"
        ) from error

    return parse_core_key_changes(decoded_text, version)


def parse_core_key_changes(markdown: str, version: str) -> CoreKeyChanges:
    """VOICEVOX COREの主要変更点Markdownを分解します。"""
    lines = normalize_newlines(markdown).split("\n")
    if len(lines) == 0:
        raise ReleaseNoteError("coreのkey-changesが空です")
    if lines[0] != f"# {version}":
        raise ReleaseNoteError(
            f"coreのkey-changesの見出しが対象バージョンと一致しません: {lines[0]}"
        )

    body_lines = lines[1:]
    change_heading_index = find_line_index(body_lines, "## 変更点")
    thanks_heading_index = find_line_index_or_none(body_lines, "## 謝辞")

    if thanks_heading_index is None:
        change_lines = body_lines[change_heading_index + 1 :]
        thanks_lines: list[str] = []
    else:
        if thanks_heading_index < change_heading_index:
            raise ReleaseNoteError("coreのkey-changesの章順が不正です")
        change_lines = body_lines[change_heading_index + 1 : thanks_heading_index]
        thanks_lines = body_lines[thanks_heading_index + 1 :]

    change_text, change_references = split_reference_definitions(change_lines)
    thanks_text, thanks_references = split_reference_definitions(thanks_lines)
    reference_text = join_unique_reference_definitions(
        change_references + filter_core_reference_definitions(thanks_references)
    )

    if len(change_text) == 0:
        raise ReleaseNoteError("coreのkey-changesの変更点が空です")

    return CoreKeyChanges(
        change_text=change_text,
        reference_text=reference_text,
        thanks_text=simplify_core_thanks_text(thanks_text),
    )


def build_editor_asset_lines(
    repository: str,
    version: str,
    assets: dict[str, Asset],
) -> list[str]:
    """エディタの配布物リンク一覧を生成します。"""
    lines: list[str] = []
    for section in build_editor_asset_sections(version):
        lines.append(section.title)
        for item in section.items:
            lines.append(
                "  * " + format_editor_asset_item(repository, version, assets, item)
            )
    return lines


def build_editor_asset_sections(version: str) -> list[EditorAssetSection]:
    """エディタの配布物セクション定義を生成します。"""
    return [
        EditorAssetSection(
            title="* **Windows**",
            items=(
                AssetLinkSpec(
                    asset_name=f"VOICEVOX-CPU.Web.Setup.{version}.exe",
                    label="インストーラー版（CPU）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"VOICEVOX.Web.Setup.{version}.exe",
                    label="インストーラー版（GPU/DirectML版）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"VOICEVOX-CUDA.Web.Setup.{version}.exe",
                    label="インストーラー版（GPU/CUDA版）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-windows-cpu-{version}.zip",
                    label="zip版（CPU）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-windows-directml-{version}.zip",
                    label="zip版（GPU/DirectML版）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-windows-nvidia-{version}.zip",
                    label="zip版（GPU/CUDA版）",
                    missing_policy="strikethrough",
                ),
            ),
        ),
        EditorAssetSection(
            title="* **Mac**",
            items=(
                AssetLinkSpec(
                    asset_name=f"VOICEVOX.{version}-x64.dmg",
                    label="インストーラー版 (CPU/x64版)",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"VOICEVOX.{version}-arm64.dmg",
                    label="インストーラー版 (CPU/arm64版)",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-macos-cpu-x64-{version}.zip",
                    label="zip版 (CPU/x64版)",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-macos-cpu-arm64-{version}.zip",
                    label="zip版 (CPU/arm64版)",
                    missing_policy="required",
                ),
            ),
        ),
        EditorAssetSection(
            title="* **Linux**",
            items=(
                AssetTextSpec(
                    text=(
                        "インストーラー版は "
                        "[VOICEVOX公式ページ](https://voicevox.hiroshiba.jp/) にあります"
                    )
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-linux-cpu-x64-{version}.tar.gz",
                    label="tar.gz版（CPU/x64）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-linux-cpu-arm64-{version}.tar.gz",
                    label="tar.gz版（CPU/arm64）",
                    missing_policy="required",
                ),
                AssetLinkSpec(
                    asset_name=f"voicevox-linux-nvidia-{version}.tar.gz",
                    label="tar.gz版（GPU/CUDA版）",
                    missing_policy="strikethrough",
                ),
            ),
        ),
    ]


def format_editor_asset_item(
    repository: str,
    version: str,
    assets: dict[str, Asset],
    item: AssetLinkSpec | AssetTextSpec,
) -> str:
    """エディタの配布物項目をMarkdownへ変換します。"""
    if isinstance(item, AssetLinkSpec):
        return format_asset_link_spec(repository, version, assets, item)
    if isinstance(item, AssetTextSpec):
        return item.text
    raise ReleaseNoteError("未対応のエディタ配布物項目です")


def collect_editor_asset_expectations(
    version: str,
    assets: dict[str, Asset],
) -> AssetExpectations:
    """エディタで想定するアセット名を集めます。"""
    allowed_names: set[str] = set()
    required_names: set[str] = set()

    for section in build_editor_asset_sections(version):
        for item in section.items:
            if isinstance(item, AssetLinkSpec):
                allowed_names.add(item.asset_name)
                if item.missing_policy == "required":
                    required_names.add(item.asset_name)

    for prefix, context, minimum_count in (
        (
            f"voicevox-{version}-x64.nsis.7z.",
            "エディタのDirectMLインストーラー",
            2,
        ),
        (
            f"voicevox-cpu-{version}-x64.nsis.7z.",
            "エディタのCPUインストーラー",
            2,
        ),
        (
            f"voicevox-cuda-{version}-x64.nsis.7z.",
            "エディタのCUDAインストーラー",
            3,
        ),
    ):
        add_required_asset_names(
            allowed_names,
            required_names,
            collect_numbered_asset_names(
                assets,
                prefix,
                "",
                context,
                True,
                0,
                None,
                minimum_count,
            ),
        )

    add_required_asset_names(
        allowed_names,
        required_names,
        {
            f"voicevox-{version}-x64.nsis.7z.ini",
            f"voicevox-cpu-{version}-x64.nsis.7z.ini",
            f"voicevox-cuda-{version}-x64.nsis.7z.ini",
            "linux-cpu-arm64-appimage.7z.txt",
            "linux-cpu-x64-appimage.7z.txt",
            "linux-nvidia-appimage.7z.txt",
        },
    )

    for prefix, context in (
        ("VOICEVOX-CPU-ARM64.AppImage.7z.", "エディタのLinux CPU arm64 AppImage"),
        ("VOICEVOX-CPU-X64.AppImage.7z.", "エディタのLinux CPU x64 AppImage"),
        ("VOICEVOX.AppImage.7z.", "エディタのLinux CUDA AppImage"),
    ):
        add_required_asset_names(
            allowed_names,
            required_names,
            collect_numbered_asset_names(
                assets,
                prefix,
                "",
                context,
                True,
                1,
                3,
                1,
            ),
        )

    allowed_names.update(
        {
            f"VOICEVOX.linux-cpu-arm64-appimage.{version}.sh",
            f"VOICEVOX.linux-cpu-x64-appimage.{version}.sh",
            f"VOICEVOX.linux-nvidia-appimage.{version}.sh",
        }
    )

    return AssetExpectations(
        allowed_names=allowed_names,
        required_names=required_names,
    )


def build_engine_asset_lines(
    repository: str,
    version: str,
    assets: dict[str, Asset],
) -> list[str]:
    """エンジンの配布物リンク一覧を生成します。"""
    lines = [
        "- エンジン本体",
    ]
    lines.extend(
        build_engine_package_group_lines(
            version,
            assets,
            "7z",
        )
    )
    lines.append("- プラグインエンジン（VVPP）")
    lines.extend(
        build_engine_package_group_lines(
            version,
            assets,
            "vvpp",
        )
    )
    return lines


def build_engine_package_group_lines(
    version: str,
    assets: dict[str, Asset],
    package_kind: EnginePackageKind,
) -> list[str]:
    """エンジン配布物の種類ごとのリンク一覧を生成します。"""
    lines: list[str] = []
    for package_spec in ENGINE_PACKAGE_SPECS:
        link = format_engine_package_link(
            version,
            assets,
            package_spec,
            package_kind,
        )
        lines.append(f"  - {link}")
    return lines


def format_engine_package_link(
    version: str,
    assets: dict[str, Asset],
    package_spec: EnginePackageSpec,
    package_kind: EnginePackageKind,
) -> str:
    """エンジンの配布物リンクを生成します。"""
    package_names = collect_engine_package_asset_names(
        version,
        assets,
        package_spec,
        package_kind,
    )
    return format_asset_link_or_parts(assets, package_spec.label, package_names)


def collect_engine_asset_expectations(
    version: str,
    assets: dict[str, Asset],
) -> AssetExpectations:
    """エンジンで想定するアセット名を集めます。"""
    allowed_names: set[str] = set()
    required_names: set[str] = set()

    for package_spec in ENGINE_PACKAGE_SPECS:
        for package_kind in ("7z", "vvpp"):
            add_required_asset_names(
                allowed_names,
                required_names,
                collect_engine_package_asset_names(
                    version,
                    assets,
                    package_spec,
                    package_kind,
                ),
            )
            add_required_asset_names(
                allowed_names,
                required_names,
                {
                    (
                        f"voicevox_engine-{package_spec.platform}-"
                        f"{version}.{package_kind}.txt"
                    )
                },
            )

    return AssetExpectations(
        allowed_names=allowed_names,
        required_names=required_names,
    )


def collect_engine_package_asset_names(
    version: str,
    assets: dict[str, Asset],
    package_spec: EnginePackageSpec,
    package_kind: EnginePackageKind,
) -> list[str]:
    """エンジン配布物のアセット名を集めます。"""
    if package_kind == "7z":
        return collect_numbered_asset_names(
            assets,
            f"voicevox_engine-{package_spec.platform}-{version}.7z.",
            "",
            f"エンジンの{package_spec.label} 7z",
            True,
            1,
            3,
            package_spec.minimum_7z_part_count,
        )
    if package_kind != "vvpp":
        raise ReleaseNoteError(f"未対応のエンジン配布物種別です: {package_kind}")

    single_name = f"voicevox_engine-{package_spec.platform}-{version}.vvpp"
    part_names = collect_numbered_asset_names(
        assets,
        f"voicevox_engine-{package_spec.platform}-{version}.",
        ".vvppp",
        f"エンジンの{package_spec.label} VVPP",
        False,
        1,
        3,
        package_spec.minimum_vvpp_part_count or 1,
    )
    has_single_asset = single_name in assets
    has_part_assets = len(part_names) > 0

    if package_spec.minimum_vvpp_part_count is None:
        if has_part_assets is True:
            raise ReleaseNoteError(
                f"エンジンの{package_spec.label} VVPPは単一アセットを想定しています"
            )
        if has_single_asset is False:
            raise ReleaseNoteError(f"リリースアセットが見つかりません: {single_name}")
        return [single_name]

    if has_single_asset is True:
        raise ReleaseNoteError(
            f"エンジンの{package_spec.label} VVPPは分割アセットを想定しています"
        )
    if has_part_assets is False:
        raise ReleaseNoteError(f"エンジンの{package_spec.label} VVPPが見つかりません")

    return part_names


def build_core_asset_lines(
    repository: str,
    version: str,
    assets: dict[str, Asset],
) -> list[str]:
    """コアのアセット一覧を生成します。"""
    lines: list[str] = []
    lines.extend(build_core_downloader_lines(repository, version, assets))
    append_blank_line(lines)
    lines.extend(build_core_c_api_lines(version, assets))
    append_blank_line(lines)
    lines.extend(build_core_python_wheel_lines(version, assets))
    return lines


def build_core_downloader_lines(
    repository: str,
    version: str,
    assets: dict[str, Asset],
) -> list[str]:
    """コアのダウンローダー一覧を生成します。"""
    rows = [
        (
            os_name,
            architecture,
            format_required_asset_link(
                repository,
                version,
                assets,
                asset_name,
                asset_name,
            ),
        )
        for os_name, architecture, asset_name in CORE_DOWNLOADER_ASSET_ROWS
    ]
    return build_core_asset_table_lines("ダウンローダ", rows)


def build_core_c_api_lines(version: str, assets: dict[str, Asset]) -> list[str]:
    """コアのC API一覧を生成します。"""
    return build_core_asset_name_table_lines(
        "C API",
        build_core_c_api_asset_rows(version),
        assets,
    )


def build_core_c_api_asset_rows(version: str) -> list[tuple[str, str, str]]:
    """コアのC APIアセット行を生成します。"""
    return [
        ("Windows", "x64", f"voicevox_core-windows-x64-{version}.zip"),
        ("Windows", "x86", f"voicevox_core-windows-x86-{version}.zip"),
        ("macOS", "x64", f"voicevox_core-osx-x64-{version}.zip"),
        ("macOS", "arm64", f"voicevox_core-osx-arm64-{version}.zip"),
        ("Linux", "x64", f"voicevox_core-linux-x64-{version}.zip"),
        ("Linux", "arm64", f"voicevox_core-linux-arm64-{version}.zip"),
        ("Android", "arm64", f"voicevox_core-android-arm64-{version}.zip"),
        ("Android", "x86_64", f"voicevox_core-android-x86_64-{version}.zip"),
        ("iOS", "-", f"voicevox_core-ios-xcframework-cpu-{version}.zip"),
    ]


def build_core_python_wheel_lines(version: str, assets: dict[str, Asset]) -> list[str]:
    """コアのPython wheel一覧を生成します。"""
    return build_core_asset_name_table_lines(
        "Python wheel",
        build_core_python_wheel_asset_rows(version),
        assets,
    )


def build_core_python_wheel_asset_rows(version: str) -> list[tuple[str, str, str]]:
    """コアのPython wheelアセット行を生成します。"""
    return [
        (
            "Windows",
            "x86_64",
            f"voicevox_core-{version}-cp310-abi3-win_amd64.whl",
        ),
        (
            "Windows",
            "x86",
            f"voicevox_core-{version}-cp310-abi3-win32.whl",
        ),
        (
            "macOS",
            "x86_64",
            f"voicevox_core-{version}-cp310-abi3-macosx_10_12_x86_64.whl",
        ),
        (
            "macOS",
            "arm64",
            f"voicevox_core-{version}-cp310-abi3-macosx_11_0_arm64.whl",
        ),
        (
            "Linux",
            "x86_64",
            f"voicevox_core-{version}-cp310-abi3-manylinux_2_34_x86_64.whl",
        ),
        (
            "Linux",
            "aarch64",
            f"voicevox_core-{version}-cp310-abi3-manylinux_2_34_aarch64.whl",
        ),
    ]


def collect_core_asset_expectations(version: str) -> AssetExpectations:
    """コアで想定するアセット名を集めます。"""
    required_names = {"java_packages.zip"}
    for rows in (
        CORE_DOWNLOADER_ASSET_ROWS,
        build_core_c_api_asset_rows(version),
        build_core_python_wheel_asset_rows(version),
    ):
        required_names.update(
            asset_name for _os_name, _architecture, asset_name in rows
        )

    return AssetExpectations(
        allowed_names=set(required_names),
        required_names=required_names,
    )


def build_core_asset_name_table_lines(
    details_summary: str,
    rows: list[tuple[str, str, str]],
    assets: dict[str, Asset],
) -> list[str]:
    """アセット名を表示するcoreの表を生成します。"""
    table_rows: list[tuple[str, str, str]] = []
    for os_name, architecture, asset_name in rows:
        ensure_asset_exists(assets, asset_name)
        table_rows.append((os_name, architecture, asset_name))
    return build_core_asset_table_lines(details_summary, table_rows)


def build_core_asset_table_lines(
    details_summary: str,
    rows: list[tuple[str, str, str]],
) -> list[str]:
    """coreのアセット表をdetails付きで生成します。"""
    return [
        f"<details><summary>{details_summary}</summary>",
        "",
        *format_markdown_table(CORE_ASSET_TABLE_HEADERS, rows),
        "</details>",
    ]


def format_asset_link(
    repository: str,
    version: str,
    assets: dict[str, Asset],
    label: str,
    asset_name: str,
    missing_policy: AssetMissingPolicy,
) -> str:
    """単一アセットへのMarkdownリンクを生成します。"""
    if asset_name in assets:
        asset = assets[asset_name]
        return f"[{label}]({asset.url})"

    if missing_policy == "required":
        raise ReleaseNoteError(f"リリースアセットが見つかりません: {asset_name}")

    if missing_policy == "strikethrough":
        url = build_release_asset_url(repository, version, asset_name)
        link_text = f"[{label}]({url})"
        return f"<s>{link_text}</s>"

    raise ReleaseNoteError(f"未対応のアセット欠落時の処理です: {missing_policy}")


def format_asset_link_spec(
    repository: str,
    version: str,
    assets: dict[str, Asset],
    link_spec: AssetLinkSpec,
) -> str:
    """アセットリンク設定からMarkdownリンクを生成します。"""
    return format_asset_link(
        repository,
        version,
        assets,
        link_spec.label,
        link_spec.asset_name,
        link_spec.missing_policy,
    )


def format_required_asset_link(
    repository: str,
    version: str,
    assets: dict[str, Asset],
    label: str,
    asset_name: str,
) -> str:
    """必須アセットへのMarkdownリンクを生成します。"""
    return format_asset_link(
        repository,
        version,
        assets,
        label,
        asset_name,
        "required",
    )


def format_asset_link_or_parts(
    assets: dict[str, Asset],
    label: str,
    asset_names: list[str],
) -> str:
    """単一ファイルまたは分割ファイルのMarkdownリンクを生成します。"""
    if len(asset_names) == 0:
        raise ReleaseNoteError(f"{label} のアセットが空です")

    for asset_name in asset_names:
        ensure_asset_exists(assets, asset_name)

    if len(asset_names) == 1:
        asset = assets[asset_names[0]]
        return f"[{label}]({asset.url})"

    part_links = [
        f"[part{index + 1}]({assets[asset_name].url})"
        for index, asset_name in enumerate(asset_names)
    ]
    return f"{label} {' '.join(part_links)}"


def collect_numbered_asset_names(
    assets: dict[str, Asset],
    prefix: str,
    suffix: str,
    context: str,
    required: bool,
    first_number: int,
    number_width: int | None,
    minimum_count: int,
) -> list[str]:
    """番号付き分割アセット名を番号順に集めます。"""
    if first_number < 0:
        raise ReleaseNoteError(f"{context} の開始番号が不正です")
    if number_width is not None and number_width <= 0:
        raise ReleaseNoteError(f"{context} の番号桁数が不正です")
    if minimum_count <= 0:
        raise ReleaseNoteError(f"{context} の最小分割数が不正です")

    pattern = re.compile(f"^{re.escape(prefix)}(?P<number>[0-9]+){re.escape(suffix)}$")
    numbered_asset_names: list[tuple[int, str]] = []
    for asset_name in assets:
        match = pattern.fullmatch(asset_name)
        if match is None:
            continue

        number_text = match.group("number")
        number = int(number_text)
        if number_width is None:
            expected_number_text = str(number)
        else:
            expected_number_text = f"{number:0{number_width}d}"
        if number_text != expected_number_text:
            raise ReleaseNoteError(
                f"{context} の分割番号の形式が不正です: {asset_name}"
            )
        numbered_asset_names.append((number, asset_name))

    if len(numbered_asset_names) == 0:
        if required is True:
            raise ReleaseNoteError(f"{context} の分割アセットが見つかりません")
        return []

    numbered_asset_names.sort()
    actual_numbers = [number for number, _asset_name in numbered_asset_names]
    expected_numbers = list(
        range(first_number, first_number + len(numbered_asset_names))
    )
    if actual_numbers != expected_numbers:
        asset_names = [asset_name for _number, asset_name in numbered_asset_names]
        raise ReleaseNoteError(
            f"{context} の分割番号が連番ではありません: {', '.join(asset_names)}"
        )
    if len(numbered_asset_names) < minimum_count:
        asset_names = [asset_name for _number, asset_name in numbered_asset_names]
        raise ReleaseNoteError(
            f"{context} の分割アセット数が不足しています: {', '.join(asset_names)}"
        )
    return [asset_name for _number, asset_name in numbered_asset_names]


def add_required_asset_names(
    allowed_names: set[str],
    required_names: set[str],
    asset_names: set[str] | list[str],
) -> None:
    """必須アセット名を許可アセット名にも追加します。"""
    allowed_names.update(asset_names)
    required_names.update(asset_names)


def validate_expected_assets(
    assets: dict[str, Asset],
    expectations: AssetExpectations,
    context: str,
) -> None:
    """必須アセットの欠落と想定外アセットの混入を確認します。"""
    unknown_required_names = sorted(
        asset_name
        for asset_name in expectations.required_names
        if asset_name not in expectations.allowed_names
    )
    if len(unknown_required_names) > 0:
        raise ReleaseNoteError(
            f"{context}の期待アセット定義が不正です: {', '.join(unknown_required_names)}"
        )

    missing_names = sorted(
        asset_name
        for asset_name in expectations.required_names
        if asset_name not in assets
    )
    if len(missing_names) > 0:
        raise ReleaseNoteError(
            f"{context}に必要なリリースアセットがありません: {', '.join(missing_names)}"
        )

    unexpected_names = sorted(
        asset_name
        for asset_name in assets
        if asset_name not in expectations.allowed_names
    )
    if len(unexpected_names) > 0:
        raise ReleaseNoteError(
            f"{context}に想定外のリリースアセットがあります: {', '.join(unexpected_names)}"
        )


def ensure_asset_exists(assets: dict[str, Asset], asset_name: str) -> None:
    """アセットの存在を確認します。"""
    if asset_name not in assets:
        raise ReleaseNoteError(f"リリースアセットが見つかりません: {asset_name}")


def format_markdown_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> list[str]:
    """Markdown表を桁揃えして生成します。"""
    if len(headers) == 0:
        raise ReleaseNoteError("Markdown表のヘッダーが空です")

    widths = [display_width(header) for header in headers]
    for row_index, row in enumerate(rows):
        if len(row) != len(headers):
            raise ReleaseNoteError(f"Markdown表の {row_index} 行目の列数が不正です")
        for column_index, cell in enumerate(row):
            widths[column_index] = max(widths[column_index], display_width(cell))

    lines = [
        format_markdown_table_row(headers, widths),
        format_markdown_table_separator(widths),
    ]
    for row in rows:
        lines.append(format_markdown_table_row(row, widths))
    return lines


def format_markdown_table_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Markdown表の1行を生成します。"""
    padded_cells = [
        pad_display_width(cell, widths[index]) for index, cell in enumerate(cells)
    ]
    return "| " + " | ".join(padded_cells) + " |"


def format_markdown_table_separator(widths: Sequence[int]) -> str:
    """Markdown表の区切り行を生成します。"""
    cells = ["-" * width for width in widths]
    return "| " + " | ".join(cells) + " |"


def pad_display_width(text: str, width: int) -> str:
    """表示幅に合わせて右側を空白で埋めます。"""
    padding_width = width - display_width(text)
    if padding_width < 0:
        raise ReleaseNoteError(f"表示幅の計算が不正です: {text}")
    return text + " " * padding_width


def display_width(text: str) -> int:
    """Markdown表向けの表示幅を計算します。"""
    width = 0
    for character in text:
        east_asian_width = unicodedata.east_asian_width(character)
        if east_asian_width == "F" or east_asian_width == "W":
            width += 2
        else:
            width += 1
    return width


def build_release_asset_url(repository: str, version: str, asset_name: str) -> str:
    """GitHub ReleaseアセットのURLを組み立てます。"""
    return f"https://github.com/{repository}/releases/download/{version}/{asset_name}"


def append_optional_generated_notes(lines: list[str], generated_notes: str) -> None:
    """自動生成リリースノートがあれば追記します。"""
    if len(generated_notes.strip()) > 0:
        append_blank_line(lines)
        append_markdown_block(lines, generated_notes)


def append_markdown_block(lines: list[str], markdown: str) -> None:
    """Markdown断片を行リストへ追加します。"""
    block_lines = trim_surrounding_blank_lines(normalize_newlines(markdown).split("\n"))
    lines.extend(block_lines)


def append_blank_line(lines: list[str]) -> None:
    """空行を追加します。"""
    if len(lines) == 0 or lines[len(lines) - 1] != "":
        lines.append("")


def render_lines(lines: list[str]) -> str:
    """行リストをOS標準改行の文字列にします。"""
    normalized_lines = remove_trailing_blank_lines(lines)
    return os.linesep.join(normalized_lines) + os.linesep


def normalize_newlines(text: str) -> str:
    """改行を内部表現へ正規化します。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def trim_surrounding_blank_lines(lines: list[str]) -> list[str]:
    """前後の空行を取り除きます。"""
    start = 0
    end = len(lines)

    while start < end and len(lines[start].strip()) == 0:
        start += 1

    while end > start and len(lines[end - 1].strip()) == 0:
        end -= 1

    return lines[start:end]


def remove_trailing_blank_lines(lines: list[str]) -> list[str]:
    """末尾の空行を取り除きます。"""
    end = len(lines)
    while end > 0 and len(lines[end - 1].strip()) == 0:
        end -= 1
    return lines[0:end]


def split_reference_definitions(lines: list[str]) -> tuple[str, list[str]]:
    """Markdown本文と参照リンク定義を分離します。"""
    body_lines: list[str] = []
    reference_lines: list[str] = []
    for line in lines:
        if is_reference_definition(line) is True:
            reference_lines.append(line)
        else:
            body_lines.append(line)

    body_text = "\n".join(trim_surrounding_blank_lines(body_lines))
    return body_text, reference_lines


def simplify_core_thanks_text(thanks_text: str) -> str:
    """coreの謝辞をRelease本文向けに整えます。"""
    simplified_text = re.sub(r"\[(@[^\]]+)\]", r"\1", thanks_text)
    simplified_text = re.sub(r"\[(#[0-9]+)\]", r"\1", simplified_text)
    return re.sub(r"(#[0-9]+)の", r"\1 の", simplified_text)


def filter_core_reference_definitions(reference_lines: list[str]) -> list[str]:
    """coreのRelease本文に残す参照リンク定義を選びます。"""
    filtered_lines: list[str] = []
    for line in reference_lines:
        stripped_line = line.strip()
        if (
            stripped_line.startswith("[@") is False
            and stripped_line.startswith("[#") is False
        ):
            filtered_lines.append(line)
    return filtered_lines


def is_reference_definition(line: str) -> bool:
    """Markdownの参照リンク定義かどうかを判定します。"""
    stripped_line = line.strip()
    return stripped_line.startswith("[") and "]:" in stripped_line


def join_unique_reference_definitions(reference_lines: list[str]) -> str:
    """参照リンク定義を重複なしで結合します。"""
    unique_lines: list[str] = []
    seen_lines: set[str] = set()
    for line in reference_lines:
        if line not in seen_lines:
            seen_lines.add(line)
            unique_lines.append(line)
    return "\n".join(unique_lines)


def find_line_index(lines: list[str], expected_line: str) -> int:
    """一致する行の位置を返します。"""
    index = find_line_index_or_none(lines, expected_line)
    if index is None:
        raise ReleaseNoteError(f"必要な見出しが見つかりません: {expected_line}")
    return index


def find_line_index_or_none(lines: list[str], expected_line: str) -> int | None:
    """一致する行の位置があれば返します。"""
    for index, line in enumerate(lines):
        if line == expected_line:
            return index
    return None


def run_gh_json(command: list[str]) -> object:
    """ghコマンドを実行してJSONを返します。"""
    completed_process = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed_process.returncode != 0:
        stderr = completed_process.stderr.strip()
        stdout = completed_process.stdout.strip()
        detail = stderr if len(stderr) > 0 else stdout
        if len(detail) == 0:
            detail = "詳細なし"
        raise ReleaseNoteError(f"ghコマンドに失敗しました: {detail}")

    try:
        return json.loads(completed_process.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseNoteError("ghコマンドのJSONを解析できませんでした") from error


def expect_namespace_str(args: argparse.Namespace, name: str) -> str:
    """argparseの文字列値を取得します。"""
    value = getattr(args, name)
    if not isinstance(value, str):
        raise ReleaseNoteError(f"{name} の値が不正です")
    return value


def expect_namespace_optional_str(args: argparse.Namespace, name: str) -> str | None:
    """argparseの任意文字列値を取得します。"""
    value = getattr(args, name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseNoteError(f"{name} の値が不正です")
    return value


def expect_namespace_optional_path(args: argparse.Namespace, name: str) -> Path | None:
    """argparseの任意パス値を取得します。"""
    value = getattr(args, name)
    if value is None:
        return None
    if not isinstance(value, Path):
        raise ReleaseNoteError(f"{name} の値が不正です")
    return value


def expect_namespace_optional_str_list(
    args: argparse.Namespace,
    name: str,
) -> list[str] | None:
    """argparseの任意文字列リスト値を取得します。"""
    value = getattr(args, name)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ReleaseNoteError(f"{name} の値が不正です")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ReleaseNoteError(f"{name} の {index} 番目の値が不正です")
    return value


def expect_mapping_str(
    mapping: dict[object, object],
    key: str,
    context: str,
) -> str:
    """辞書から文字列値を取得します。"""
    if key not in mapping:
        raise ReleaseNoteError(f"{context} に {key} がありません")
    value = mapping[key]
    if not isinstance(value, str):
        raise ReleaseNoteError(f"{context} の {key} が文字列ではありません")
    return value


def expect_mapping_bool(
    mapping: dict[object, object],
    key: str,
    context: str,
) -> bool:
    """辞書から真偽値を取得します。"""
    if key not in mapping:
        raise ReleaseNoteError(f"{context} に {key} がありません")
    value = mapping[key]
    if not isinstance(value, bool):
        raise ReleaseNoteError(f"{context} の {key} が真偽値ではありません")
    return value


def expect_mapping_list(
    mapping: dict[object, object],
    key: str,
    context: str,
) -> list[object]:
    """辞書からリスト値を取得します。"""
    if key not in mapping:
        raise ReleaseNoteError(f"{context} に {key} がありません")
    value = mapping[key]
    if not isinstance(value, list):
        raise ReleaseNoteError(f"{context} の {key} がリストではありません")
    return value


def write_body(body: str, output_path: Path | None) -> None:
    """生成した本文を出力します。"""
    if output_path is None:
        stdout_buffer = getattr(sys.stdout, "buffer", None)
        if stdout_buffer is None:
            sys.stdout.write(body)
        else:
            stdout_buffer.write(body.encode("utf-8"))
        return

    try:
        output_path.write_text(body, encoding="utf-8", newline="")
    except OSError as error:
        raise ReleaseNoteError(
            f"出力ファイルを書き込めませんでした: {output_path}"
        ) from error


if __name__ == "__main__":
    main()
