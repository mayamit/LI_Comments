from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import tones as tones_store

router = APIRouter(prefix="/admin/tones", tags=["tones"])
templates = Jinja2Templates(directory="templates")


async def _render(
    request: Request,
    *,
    full_page: bool,
    edit_key: Optional[str] = None,
    show_new: bool = False,
    error: Optional[str] = None,
    flash: Optional[str] = None,
):
    data = tones_store.load()
    ctx = {
        "shared_system_prompt": data["shared_system_prompt"],
        "tones": data["tones"],
        "edit_key": edit_key,
        "show_new": show_new,
        "error": error,
        "flash": flash,
    }
    template = "tones.html" if full_page else "_tones_main.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("", response_class=HTMLResponse)
async def tones_page(request: Request, edit: Optional[str] = None, new: bool = False):
    return await _render(request, full_page=True, edit_key=edit, show_new=new)


@router.post("", response_class=HTMLResponse)
async def create_tone(
    request: Request,
    key: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    tone_prompt: str = Form(...),
):
    try:
        tones_store.create(key.strip(), name, description, tone_prompt)
    except tones_store.ToneError as e:
        return await _render(request, full_page=False, show_new=True, error=str(e))
    return await _render(request, full_page=False, flash=f"Added tone '{key}'.")


@router.get("/new", response_class=HTMLResponse)
async def new_tone_form(request: Request):
    return await _render(request, full_page=False, show_new=True)


@router.get("/{key}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, key: str):
    return await _render(request, full_page=False, edit_key=key)


@router.post("/{key}/edit", response_class=HTMLResponse)
async def save_edit(
    request: Request,
    key: str,
    name: str = Form(...),
    description: str = Form(""),
    tone_prompt: str = Form(...),
):
    try:
        tones_store.update(key, name, description, tone_prompt)
    except tones_store.ToneError as e:
        return await _render(request, full_page=False, edit_key=key, error=str(e))
    return await _render(request, full_page=False, flash=f"Updated tone '{key}'.")


@router.post("/{key}/delete", response_class=HTMLResponse)
async def delete_tone(request: Request, key: str):
    try:
        tones_store.delete(key)
    except tones_store.ToneError as e:
        return await _render(request, full_page=False, error=str(e))
    return await _render(request, full_page=False, flash=f"Deleted tone '{key}'.")


@router.post("/shared", response_class=HTMLResponse)
async def save_shared_prompt(request: Request, shared_system_prompt: str = Form(...)):
    tones_store.update_shared_prompt(shared_system_prompt)
    return await _render(request, full_page=False, flash="Shared system prompt updated.")
