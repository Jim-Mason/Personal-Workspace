#!/usr/bin/env python3
"""OpsGen - Operations Script Generator Web Application."""

import io
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for
from flask_socketio import SocketIO, emit

from engine.diff_utils import compare_payload
from engine.generator import ScriptGenerator, ValidationError
from engine.loader import TemplateError, TemplateLoader
from engine.template_starter import CUSTOM_TEMPLATE_STARTER
from services.batch import BatchService
from services.favorites import FavoritesService
from services.history import HistoryService
from services.share import ShareService
from services.versions import VersionService

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
CUSTOM_TEMPLATES_DIR = BASE_DIR / "data" / "custom_templates"
WEB_TEMPLATES_DIR = BASE_DIR / "web_templates"
DATA_DIR = BASE_DIR / "data"
SHARES_DIR = DATA_DIR / "shares"
VERSIONS_DIR = DATA_DIR / "template_versions"
BATCHES_DIR = DATA_DIR / "batches"

app = Flask(__name__, template_folder=str(WEB_TEMPLATES_DIR), static_folder=str(BASE_DIR / "static"))
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "opsgen-dev-secret-key")

loader = TemplateLoader(TEMPLATES_DIR, CUSTOM_TEMPLATES_DIR)
generator = ScriptGenerator(loader)
history_service = HistoryService(DATA_DIR / "history.json")
favorites_service = FavoritesService(DATA_DIR / "favorites.json")
share_service = ShareService(SHARES_DIR)
version_service = VersionService(VERSIONS_DIR)
batch_service = BatchService(BATCHES_DIR)


def _default_params(template: dict) -> dict:
    return generator._normalize_params(template, {})


def _search_context_from_request():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    source = request.args.get("source", "").strip()
    sort_by = request.args.get("sort", "title").strip() or "title"
    favorites_only = request.args.get("fav", "") == "1"
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 12, type=int)
    return {
        "query": query or None,
        "category": category or None,
        "source": source or None,
        "sort_by": sort_by,
        "favorites_only": favorites_only,
        "page": page,
        "per_page": per_page,
        "filters": {
            "q": query,
            "category": category,
            "source": source,
            "sort": sort_by,
            "fav": "1" if favorites_only else "",
            "per_page": per_page,
        },
    }

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def _generate_with_services(template_name: str, params: dict, create_share: bool = True):
    result = generator.generate(template_name, params)
    share_id = None
    if create_share:
        share_id = share_service.create_share(
            template=result["template"],
            title=result["title"],
            params=result["params"],
            outputs=result["outputs"],
        )
    history_service.add_entry(
        template=result["template"],
        title=result["title"],
        params=result["params"],
        outputs=result["outputs"],
        share_id=share_id,
    )
    result["share_id"] = share_id
    return result


def _save_template_version(name: str, note: str = "保存前快照") -> None:
    try:
        content = loader.get_template_raw(name)
        version_service.save_version(name, content, note=note)
    except FileNotFoundError:
        pass


def _resolve_compare(mode: str, **kwargs) -> dict:
    if mode == "template":
        name = kwargs["name"]
        v1 = kwargs.get("v1", "current")
        v2 = kwargs.get("v2", "")
        if v1 == "current":
            text_a = loader.get_template_raw(name)
            label_a = f"{name} (当前)"
        else:
            text_a = version_service.get_version_content(name, v1)
            label_a = f"{name} ({v1})"
            if text_a is None:
                raise ValueError(f"版本 {v1} 不存在")

        if v2 == "current":
            text_b = loader.get_template_raw(name)
            label_b = f"{name} (当前)"
        else:
            text_b = version_service.get_version_content(name, v2)
            label_b = f"{name} ({v2})"
            if text_b is None:
                raise ValueError(f"版本 {v2} 不存在")
        return compare_payload(label_a, text_a, label_b, text_b)

    if mode == "history":
        entry_a = history_service.get_entry(kwargs["id1"])
        entry_b = history_service.get_entry(kwargs["id2"])
        if not entry_a or not entry_b:
            raise ValueError("历史记录不存在")
        text_a = entry_a["outputs"].get("script", "")
        text_b = entry_b["outputs"].get("script", "")
        label_a = f"{entry_a['title']} ({entry_a['created_at'][:19]})"
        label_b = f"{entry_b['title']} ({entry_b['created_at'][:19]})"
        return compare_payload(label_a, text_a, label_b, text_b)

    raise ValueError("不支持的对比模式")


@app.route("/")
def index():
    ctx = _search_context_from_request()
    favorites = favorites_service.list_favorites()
    result = loader.search_templates(
        favorite_names=favorites,
        **{k: ctx[k] for k in ("query", "category", "source", "page", "per_page", "sort_by", "favorites_only")},
    )
    for item in result["items"]:
        item["favorite"] = item["name"] in favorites
    history = history_service.list_entries()["items"]
    return render_template(
        "index.html",
        templates=result["items"],
        pagination=result,
        categories=loader.get_categories(),
        filters=ctx["filters"],
        favorites=favorites,
        history=history,
    )


@app.route("/dashboard")
def dashboard():
    stats = history_service.get_stats()
    favorites = favorites_service.list_favorites()
    favorite_templates = [
        item
        for item in loader.search_templates(favorite_names=favorites, per_page=100)["items"]
        if item["name"] in favorites
    ]
    return render_template(
        "dashboard.html",
        stats=stats,
        favorites=favorite_templates,
        total_templates=loader.search_templates(per_page=1)["total"],
    )


@app.route("/templates/new", methods=["GET", "POST"])
def template_create():
    if request.method == "POST":
        yaml_content = request.form.get("yaml_content", "")
        try:
            loader.save_template(yaml_content)
            template = yaml.safe_load(yaml_content) or {}
            name = template.get("name", "")
            version_service.save_version(name, yaml_content, note="首次创建")
            return redirect(url_for("template_form", name=name))
        except (TemplateError, ValueError) as exc:
            return render_template(
                "template_editor.html",
                mode="create",
                yaml_content=yaml_content,
                error=str(exc),
            )

    starter = request.args.get("starter", "blank")
    content = CUSTOM_TEMPLATE_STARTER if starter != "example" else loader.get_template_raw("_example_custom")
    return render_template("template_editor.html", mode="create", yaml_content=content, error=None)


@app.route("/templates/<name>/edit", methods=["GET", "POST"])
def template_edit(name):
    if not loader.is_custom(name):
        return render_template("404.html", message="内置模板不可编辑，请复制后创建自定义模板"), 404

    if request.method == "POST":
        yaml_content = request.form.get("yaml_content", "")
        try:
            _save_template_version(name, note="编辑前自动备份")
            loader.update_template(name, yaml_content)
            updated = yaml.safe_load(yaml_content) or {}
            new_name = updated.get("name", name)
            if new_name != name:
                version_service.delete_versions(name)
            return redirect(url_for("index", q=new_name))
        except (TemplateError, ValueError) as exc:
            return render_template(
                "template_editor.html",
                mode="edit",
                template_name=name,
                yaml_content=yaml_content,
                error=str(exc),
            )

    try:
        content = loader.get_template_raw(name)
    except FileNotFoundError:
        return render_template("404.html"), 404
    return render_template(
        "template_editor.html",
        mode="edit",
        template_name=name,
        yaml_content=content,
        error=None,
    )


@app.route("/templates/<name>/delete", methods=["POST"])
def template_delete(name):
    try:
        loader.delete_template(name)
        version_service.delete_versions(name)
        return redirect(url_for("index"))
    except TemplateError as exc:
        return render_template("404.html", message=str(exc)), 400


@app.route("/templates/<name>/versions")
def template_versions(name):
    try:
        template = loader.get_template(name)
    except FileNotFoundError:
        return render_template("404.html"), 404
    versions = version_service.list_versions(name)
    return render_template(
        "versions.html",
        template=template,
        versions=versions,
    )


@app.route("/compare")
def compare_page():
    mode = request.args.get("mode", "template")
    name = request.args.get("name", "")
    version_list = []
    if name:
        try:
            loader.get_template(name)
            version_list = version_service.list_versions(name)
        except FileNotFoundError:
            pass
    try:
        if mode == "template":
            v1 = request.args.get("v1", "current")
            v2 = request.args.get("v2", "")
            if not name or not v2:
                return render_template(
                    "compare.html",
                    error=None if not request.args else "请选择要对比的两个版本",
                    result=None,
                    mode=mode,
                    version_service_list=version_list,
                    history_entries=history_service.list_entries(per_page=50)["items"],
                )
            result = _resolve_compare("template", name=name, v1=v1, v2=v2)
        elif mode == "history":
            id1 = request.args.get("id1", "")
            id2 = request.args.get("id2", "")
            if not id1 or not id2:
                return render_template(
                    "compare.html",
                    error=None if not request.args else "请选择两条历史记录",
                    result=None,
                    mode=mode,
                    version_service_list=version_list,
                    history_entries=history_service.list_entries(per_page=50)["items"],
                )
            result = _resolve_compare("history", id1=id1, id2=id2)
        else:
            return render_template(
                "compare.html",
                error="未知对比模式",
                result=None,
                mode=mode,
                version_service_list=version_list,
                history_entries=[],
            )
        return render_template(
            "compare.html",
            result=result,
            error=None,
            mode=mode,
            version_service_list=version_list,
            history_entries=history_service.list_entries(per_page=50)["items"],
        )
    except (ValueError, FileNotFoundError) as exc:
        return render_template(
            "compare.html",
            error=str(exc),
            result=None,
            mode=mode,
            version_service_list=version_list,
            history_entries=history_service.list_entries(per_page=50)["items"],
        )


@app.route("/templates/<name>/fork", methods=["POST"])
def template_fork(name):
    try:
        new_name = loader.fork_template(name)
        return redirect(url_for("template_edit", name=new_name))
    except (TemplateError, FileNotFoundError) as exc:
        return render_template("404.html", message=str(exc)), 400


@app.route("/templates/<name>/export")
def template_export(name):
    try:
        raw = loader.get_template_raw(name)
    except FileNotFoundError:
        return render_template("404.html"), 404
    return Response(
        raw,
        mimetype="text/yaml",
        headers={"Content-Disposition": f"attachment; filename={name}.yaml"},
    )


@app.route("/template/<name>/quick")
def template_quick(name):
    try:
        template = loader.get_template(name)
    except FileNotFoundError:
        return render_template("404.html"), 404
    try:
        result = _generate_with_services(name, _default_params(template))
        return redirect(url_for("result_page", entry_id=result["share_id"]))
    except ValidationError as exc:
        return render_template("form.html", template=template, prefilled={}, error=str(exc))


@app.route("/template/<name>", methods=["GET", "POST"])
def template_form(name):
    try:
        template = loader.get_template(name)
    except FileNotFoundError:
        return render_template("404.html"), 404

    prefilled = {}
    for key, value in request.args.items():
        prefilled[key] = value

    # Coerce URL query params to bool/int for non-interactive mode
    for question in template.get("questions", []):
        qname = question["name"]
        if qname not in prefilled:
            continue
        qtype = question.get("type", "string")
        raw = prefilled[qname]
        if qtype == "bool":
            prefilled[qname] = str(raw).lower() in {"1", "true", "yes", "on"}
        elif qtype == "int":
            try:
                prefilled[qname] = int(raw)
            except (TypeError, ValueError):
                pass
        elif qtype == "multi" and isinstance(raw, str):
            prefilled[qname] = [item.strip() for item in raw.split(",") if item.strip()]

    if request.method == "POST":
        params = _extract_form_params(template, request.form)
        try:
            result = _generate_with_services(name, params)
            return redirect(url_for("result_page", entry_id=result["share_id"]))
        except ValidationError as exc:
            return render_template(
                "form.html",
                template=template,
                prefilled=params,
                error=str(exc),
            )

    if prefilled:
        merged = {}
        for question in template.get("questions", []):
            qname = question["name"]
            if qname in prefilled:
                merged[qname] = prefilled[qname]
            elif "default" in question:
                merged[qname] = question["default"]
        prefilled = merged

    return render_template("form.html", template=template, prefilled=prefilled, error=None)


@app.route("/template/<name>/preview")
def template_preview_page(name):
    try:
        template = loader.get_template(name)
    except FileNotFoundError:
        return render_template("404.html"), 404
    try:
        result = generator.generate(name, _default_params(template))
        return render_template(
            "result.html",
            title=f"{template['title']}（预览）",
            template_name=name,
            params=result["params"],
            outputs=result["outputs"],
            share_id=None,
            primary_output=result.get("primary_output", "script"),
            is_preview=True,
        )
    except ValidationError as exc:
        return render_template("form.html", template=template, prefilled={}, error=str(exc))


def _extract_form_params(template: dict, form) -> dict:
    params = {}
    for question in template.get("questions", []):
        name = question["name"]
        qtype = question.get("type", "string")
        if qtype == "bool":
            params[name] = name in form
        elif qtype == "multi":
            params[name] = form.getlist(name)
        else:
            value = form.get(name)
            if value is not None:
                params[name] = value
    return params


@app.route("/result/<entry_id>")
def result_page(entry_id):
    share = share_service.get_share(entry_id)
    if share:
        return render_template(
            "result.html",
            title=share["title"],
            template_name=share["template"],
            params=share["params"],
            outputs=share["outputs"],
            share_id=share["id"],
            primary_output="script",
            is_preview=False,
        )

    history_entry = history_service.get_entry(entry_id)
    if history_entry:
        return render_template(
            "result.html",
            title=history_entry["title"],
            template_name=history_entry["template"],
            params=history_entry["params"],
            outputs=history_entry["outputs"],
            share_id=history_entry.get("share_id"),
            primary_output="script",
            is_preview=False,
        )

    return render_template("404.html"), 404


@app.route("/share/<share_id>")
def share_page(share_id):
    share = share_service.get_share(share_id)
    if not share:
        return render_template("404.html"), 404
    return render_template(
        "share.html",
        share=share,
    )


@app.route("/history")
def history_page():
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    result = history_service.list_entries(query=query or None, page=page, per_page=20)
    return render_template("history.html", history=result["items"], pagination=result, query=query)


@app.route("/history/<entry_id>/delete", methods=["POST"])
def history_delete(entry_id):
    history_service.delete_entry(entry_id)
    return redirect(url_for("history_page"))


@app.route("/history/clear", methods=["POST"])
def history_clear():
    history_service.clear_all()
    return redirect(url_for("history_page"))


@app.route("/batch", methods=["GET", "POST"])
def batch_page():
    favorites = favorites_service.list_favorites()
    if request.method == "POST":
        selected = request.form.getlist("templates")
        if not selected:
            return render_template("batch.html", templates=loader.list_templates(), favorites=favorites_service.list_favorites(), error="请至少选择一个模板")
        if len(selected) > 30:
            return render_template("batch.html", templates=loader.list_templates(), favorites=favorites_service.list_favorites(), error="单次最多生成 30 个模板")

        items = []
        for name in selected:
            try:
                template = loader.get_template(name)
                result = generator.generate(name, _default_params(template))
                share_id = share_service.create_share(
                    template=result["template"],
                    title=result["title"],
                    params=result["params"],
                    outputs=result["outputs"],
                )
                history_service.add_entry(
                    template=result["template"],
                    title=result["title"],
                    params=result["params"],
                    outputs=result["outputs"],
                    share_id=share_id,
                )
                items.append(
                    {
                        "ok": True,
                        "template": name,
                        "title": result["title"],
                        "share_id": share_id,
                        "outputs": result["outputs"],
                    }
                )
            except (FileNotFoundError, ValidationError) as exc:
                items.append({"ok": False, "template": name, "error": str(exc)})

        batch = batch_service.create_batch(items)
        return redirect(url_for("batch_result_page", batch_id=batch["id"]))

    return render_template("batch.html", templates=loader.list_templates(), favorites=favorites, error=None)


@app.route("/batch/result/<batch_id>")
def batch_result_page(batch_id):
    batch = batch_service.get_batch(batch_id)
    if not batch:
        return render_template("404.html"), 404
    return render_template("batch_result.html", batch=batch)


@app.route("/api/templates", methods=["GET", "POST"])
def api_templates():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        content = data.get("yaml_content", "")
        overwrite = bool(data.get("overwrite", False))
        try:
            template = loader.save_template(content, overwrite=overwrite)
            return jsonify(template), 201
        except TemplateError as exc:
            return jsonify({"error": str(exc)}), 400

    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    source = request.args.get("source", "").strip()
    sort_by = request.args.get("sort", "title").strip() or "title"
    favorites_only = request.args.get("fav", "") == "1"
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 12, type=int)
    favorites = favorites_service.list_favorites()
    result = loader.search_templates(
        query=query or None,
        category=category or None,
        source=source or None,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        favorite_names=favorites,
        favorites_only=favorites_only,
    )
    for item in result["items"]:
        item["favorite"] = item["name"] in favorites
    return jsonify(result)


@app.route("/api/favorites", methods=["GET"])
def api_favorites():
    return jsonify(favorites_service.list_favorites())


@app.route("/api/favorites/<name>", methods=["POST"])
def api_toggle_favorite(name):
    try:
        loader.get_template(name)
    except FileNotFoundError:
        return jsonify({"error": f"Template '{name}' not found"}), 404
    return jsonify(favorites_service.toggle(name))


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(silent=True) or {}
    template_name = data.get("template")
    params = data.get("params") or {}
    if not template_name:
        return jsonify({"error": "Missing 'template' field"}), 400
    try:
        result = generator.generate(template_name, params)
        return jsonify(result)
    except FileNotFoundError:
        return jsonify({"error": f"Template '{template_name}' not found"}), 404
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/templates/categories")
def api_template_categories():
    return jsonify(loader.get_categories())


@app.route("/api/templates/validate", methods=["POST"])
def api_validate_template():
    data = request.get_json(silent=True) or {}
    content = data.get("yaml_content", "")
    try:
        result = loader.validate_yaml(content)
        return jsonify(result)
    except TemplateError as exc:
        return jsonify({"valid": False, "error": str(exc)}), 400


@app.route("/api/templates/<name>", methods=["PUT"])
def api_update_template(name):
    data = request.get_json(silent=True) or {}
    content = data.get("yaml_content", "")
    try:
        template = loader.update_template(name, content)
        return jsonify(template)
    except TemplateError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"error": f"Template '{name}' not found"}), 404


@app.route("/api/templates/<name>", methods=["DELETE"])
def api_delete_template(name):
    try:
        loader.delete_template(name)
        return jsonify({"ok": True})
    except TemplateError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/template/<name>")
def api_template(name):
    try:
        template = loader.get_template(name)
        public_template = {
            key: template[key]
            for key in (
                "name",
                "title",
                "description",
                "icon",
                "category",
                "source",
                "questions",
                "computed",
                "extra_outputs",
                "primary_output",
            )
            if key in template
        }
        return jsonify(public_template)
    except FileNotFoundError:
        return jsonify({"error": f"Template '{name}' not found"}), 404


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(silent=True) or {}
    template_name = data.get("template")
    params = data.get("params") or {}

    if not template_name:
        return jsonify({"error": "Missing 'template' field"}), 400

    try:
        result = _generate_with_services(template_name, params)
        return jsonify(result)
    except FileNotFoundError:
        return jsonify({"error": f"Template '{template_name}' not found"}), 404
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/history")
def api_history():
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    return jsonify(history_service.list_entries(query=query or None, page=page, per_page=per_page))


@app.route("/api/stats")
def api_stats():
    return jsonify(history_service.get_stats())


@app.route("/api/templates/<name>/versions")
def api_template_versions(name):
    try:
        loader.get_template(name)
    except FileNotFoundError:
        return jsonify({"error": f"Template '{name}' not found"}), 404
    return jsonify(version_service.list_versions(name))


@app.route("/api/compare", methods=["POST"])
def api_compare():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "template")
    try:
        if mode == "template":
            result = _resolve_compare(
                "template",
                name=data.get("name"),
                v1=data.get("v1", "current"),
                v2=data.get("v2"),
            )
        elif mode == "history":
            result = _resolve_compare("history", id1=data.get("id1"), id2=data.get("id2"))
        else:
            return jsonify({"error": "Unsupported mode"}), 400
        return jsonify(result)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/batch/generate", methods=["POST"])
def api_batch_generate():
    data = request.get_json(silent=True) or {}
    template_names = data.get("templates") or []
    if not template_names:
        return jsonify({"error": "Missing 'templates' field"}), 400
    if len(template_names) > 30:
        return jsonify({"error": "Maximum 30 templates per batch"}), 400

    items = []
    for name in template_names:
        params = (data.get("params") or {}).get(name, {})
        try:
            template = loader.get_template(name)
            if not params:
                params = _default_params(template)
            result = generator.generate(name, params)
            share_id = share_service.create_share(
                template=result["template"],
                title=result["title"],
                params=result["params"],
                outputs=result["outputs"],
            )
            history_service.add_entry(
                template=result["template"],
                title=result["title"],
                params=result["params"],
                outputs=result["outputs"],
                share_id=share_id,
            )
            items.append(
                {
                    "ok": True,
                    "template": name,
                    "title": result["title"],
                    "share_id": share_id,
                    "outputs": result["outputs"],
                }
            )
        except (FileNotFoundError, ValidationError) as exc:
            items.append({"ok": False, "template": name, "error": str(exc)})

    batch = batch_service.create_batch(items)
    return jsonify(batch)


@app.route("/download/batch/<batch_id>.zip")
def download_batch_zip(batch_id):
    batch = batch_service.get_batch(batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in batch["results"]:
            if not item.get("ok"):
                continue
            folder = item["template"]
            for filename, content in item.get("outputs", {}).items():
                archive.writestr(f"{folder}/{Path(filename).name}", content)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"opsgen_batch_{batch_id}.zip",
    )


@app.route("/download/<share_id>/bundle.zip")
def download_bundle(share_id):
    share = share_service.get_share(share_id)
    if not share:
        return jsonify({"error": "Share not found"}), 404
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in share["outputs"].items():
            archive.writestr(Path(filename).name, content)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{share['template']}_scripts.zip",
    )


@app.route("/download/<share_id>/<filename>")
def download_script(share_id, filename):
    share = share_service.get_share(share_id)
    if not share:
        return jsonify({"error": "Share not found"}), 404

    content = share["outputs"].get(filename)
    if content is None:
        return jsonify({"error": "File not found"}), 404

    from flask import Response

    safe_name = Path(filename).name
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={safe_name}"},
    )


@socketio.on("execute_script")
def handle_execute_script(data):
    script = (data or {}).get("script", "")
    if not script.strip():
        emit("execution_output", {"type": "error", "data": "Script is empty"})
        emit("execution_done", {"code": 1})
        return

    emit("execution_output", {"type": "info", "data": "Starting script execution...\n"})

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tmp:
        tmp.write(script)
        tmp_path = tmp.name

    try:
        os.chmod(tmp_path, 0o755)
        process = subprocess.Popen(
            ["bash", tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            emit("execution_output", {"type": "stdout", "data": line})
        process.wait()
        emit("execution_done", {"code": process.returncode})
    except Exception as exc:
        emit("execution_output", {"type": "error", "data": str(exc)})
        emit("execution_done", {"code": 1})
    finally:
        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)
