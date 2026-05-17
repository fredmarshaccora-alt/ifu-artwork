"""G.5 e2e: model selection is required at project creation; figure
creation reuses the project's bound model.

Two contracts:
  1. The new-project modal shows the "Model" section with two tabs --
     "Import from Onshape" and "Use an existing model".  Pressing
     Create without resolving a model surfaces an inline error.
  2. When opening the new-figure modal on a project that has a
     primary_source_id, the modal shows the bound model as a read-only
     banner and does NOT render a source <select>.
"""
from __future__ import annotations


def test_new_project_modal_has_model_tabs(page):
    """Open the new-project modal -- it must show two tabs labelled
    'Import from Onshape' and 'Use an existing model'."""
    page.evaluate("location.hash = '#/'")
    page.wait_for_timeout(400)
    # Click the "+ new project" placeholder
    page.evaluate("""() => {
        const card = document.querySelector('.card.placeholder');
        if (card) card.click();
    }""")
    page.wait_for_timeout(300)
    tab_labels = page.evaluate("""() => Array.from(
        document.querySelectorAll('.modal .tab'))
        .map(t => t.textContent)""")
    assert any("Import from Onshape" in t for t in tab_labels), \
        f"missing Import tab: {tab_labels}"
    assert any("existing model" in t for t in tab_labels), \
        f"missing Existing tab: {tab_labels}"


def test_new_project_existing_model_creates_project(page):
    """Switch to 'Use an existing model', pick siderail, Create -- a
    project should be created with primary_source_id == 'siderail'."""
    page.evaluate("location.hash = '#/'")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
        const card = document.querySelector('.card.placeholder');
        if (card) card.click();
    }""")
    page.wait_for_timeout(300)
    # Switch to existing-model tab
    page.evaluate("""() => {
        const tabs = document.querySelectorAll('.modal .tab');
        for (const t of tabs) {
            if (t.textContent.includes('existing model')) {
                t.click(); return;
            }
        }
    }""")
    page.wait_for_timeout(400)
    # Set name + pick siderail in the dropdown
    page.evaluate("""() => {
        const inputs = document.querySelectorAll('.modal input');
        inputs[0].value = 'G5-model-test';
        inputs[0].dispatchEvent(new Event('input'));
        const sel = document.querySelector('.modal select');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(200)
    # Click Create (primary button in footer)
    page.evaluate("""() => {
        const btns = document.querySelectorAll('.modal .modal-footer .btn');
        for (const b of btns) {
            if (b.textContent.trim() === 'Create') { b.click(); return; }
        }
    }""")
    page.wait_for_timeout(800)
    # Now we should be on a project route
    hash_ = page.evaluate("location.hash")
    assert hash_.startswith("#/project/"), \
        f"expected project route, got {hash_!r}"
    # Fetch the project record and verify primary_source_id
    pid = hash_.split("/")[-1]
    proj = page.evaluate(f"""async () => {{
        const r = await fetch(API_BASE + '/api/projects/{pid}');
        return await r.json();
    }}""")
    try:
        assert proj.get("primary_source_id") == "siderail", \
            f"primary_source_id not set: {proj}"
        assert proj.get("name") == "G5-model-test"
    finally:
        page.evaluate(f"""async () => {{
            await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                          {{method: 'DELETE'}});
        }}""")


def test_new_figure_modal_uses_bound_model(page):
    """Create a project with primary_source_id pre-set via API; open
    the new-figure modal; it should NOT show a Source <select> --
    instead a read-only banner with the bound model name."""
    proj = page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G5-bound-test',
                                    primary_source_id: 'siderail'}),
        });
        return await r.json();
    }""")
    pid = proj["id"]
    try:
        # Phase 3: the new-figure flow now lives inside a View, not at
        # the project root.  Seed a view, navigate to its ViewScreen,
        # click the "+ New figure" placeholder.
        view = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/views', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{project_id: '{pid}',
                                        source_id: 'siderail',
                                        name: 'V-test'}}),
            }});
            return await r.json();
        }}""")
        page.evaluate(
            f"location.hash = '#/project/{pid}/view/{view['id']}'")
        page.wait_for_timeout(800)
        page.evaluate("""() => {
            const card = document.querySelector('.card.placeholder');
            if (card) card.click();
        }""")
        page.wait_for_timeout(400)
        info = page.evaluate("""() => {
            const modal = document.querySelector('.modal');
            if (!modal) return null;
            const sourceSelects = Array.from(
                modal.querySelectorAll('select')).filter(s =>
                    s.previousElementSibling
                    && s.previousElementSibling.textContent === 'Source');
            const hasNameInput = !!modal.querySelector('input.input');
            return {
                source_selects: sourceSelects.length,
                has_name_input: hasNameInput,
            };
        }""")
        assert info, "new-figure modal didn't open"
        # Phase 3: figure inherits view's camera + source; no source
        # picker.
        assert info["source_selects"] == 0, \
            f"expected no Source <select> when figure inherits view source; got {info['source_selects']}"
        assert info["has_name_input"], "missing figure-name input"
    finally:
        page.evaluate(f"""async () => {{
            await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                          {{method: 'DELETE'}});
        }}""")
