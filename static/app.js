// Shared page helpers. Kept minimal: HTMX handles everything it can.

function liCopy(btn, commentEl, postId) {
    const text = (commentEl.value !== undefined ? commentEl.value : commentEl.innerText).trim();
    navigator.clipboard.writeText(text).then(() => {
        const original = btn.innerText;
        btn.innerText = "Copied!";
        btn.classList.add("copied");
        setTimeout(() => {
            btn.innerText = original;
            btn.classList.remove("copied");
        }, 1500);
        // Mark the post reviewed in the background; ignore failures.
        fetch(`/dashboard/posts/${postId}/mark-reviewed`, { method: "POST" });
    }).catch(err => {
        btn.innerText = "Copy failed";
        console.error(err);
    });
}

// Dashboard tab currently shown (tabs push ?status= into the URL).
function liActiveStatus() {
    return new URLSearchParams(window.location.search).get("status") || "unreviewed";
}

// Copy synchronously, so it finishes before the new tab takes focus
// (navigator.clipboard needs the page focused and resolves asynchronously).
function liCopyNow(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
}

// "Copy & comment": copy this comment, open the LinkedIn post, then mark it
// posted through the normal mark-posted route. Copy and open happen inside the
// click itself so the browser allows them; the server request goes last.
function liCopyAndComment(btn) {
    const d = btn.dataset;
    if (d.confirm && !confirm(d.confirm)) return;
    const text = btn.closest(".comment-block").querySelector(".comment-text").innerText.trim();
    if (!liCopyNow(text)) {
        navigator.clipboard.writeText(text).catch(err => console.error("Copy failed", err));
    }
    const win = window.open(d.postUrl, "_blank");
    if (!win) {
        // Popup blocked: nothing opened, so don't mark it posted either.
        btn.innerText = "Popup blocked — allow popups";
        return;
    }
    win.opener = null;
    if (d.markUrl) {
        htmx.ajax("POST", d.markUrl, {
            target: "#dashboard-main",
            swap: "outerHTML",
            values: { status: liActiveStatus(), via: "copy-comment" },
        });
    } else {
        const original = btn.innerText;
        btn.innerText = "Copied & opened";
        setTimeout(() => { btn.innerText = original; }, 1500);
    }
}
