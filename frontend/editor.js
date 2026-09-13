/* Saved media edits. Caption changes preserve original voiceover and timing. */
let editingProject = null;
let editingKey = null;
let editingUploads = [];
const brandExtras = {background: "#15231d", accent: "#d1ee8a", card_color: "#ffffff"};
const sources = () => [...(editingProject.sources || editingProject.segments), ...editingUploads];
let editorRequest = 0;
const editMessage = text => { document.getElementById("edit-message").textContent = text; };

function validBrand(brand) {
  return brand && ["Arial", "DejaVu Sans", "DejaVu Serif"].includes(brand.font)
    && /^#[0-9a-f]{6}$/i.test(brand.color)
    && [64, 76, 84].includes(brand.size) && [360, 560, 800].includes(brand.position);
}

function readBrand() {
  return { font: $("brand-font").value, color: $("brand-color").value,
    ...Object.fromEntries(Object.keys(brandExtras).map(k => [k, $("brand-" + k).value || brandExtras[k]])),
    size: Number($("brand-size").value), position: Number($("brand-position").value) };
}

function showBrand(brand) {
  if (!validBrand(brand)) throw Error("Invalid brand preset. Choose valid settings and save again.");
  for (const k of Object.keys(brandExtras)) $("brand-" + k).value = brand[k] || brandExtras[k];
  $("brand-font").value = brand.font;
  $("brand-color").value = brand.color;
  $("brand-size").value = brand.size;
  $("brand-position").value = brand.position;
  $("brand-preview").style.fontFamily = brand.font;
  $("brand-preview").style.color = brand.color;
  $("brand-preview").style.fontSize = `${brand.size / 3}px`;
  $("brand-preview").style.bottom = `${brand.position / 1920 * 100}%`;
}

function currentRecipe() {
  return {
    words: [...$("caption-editor").querySelectorAll("input")].map(input => input.value.trim()),
    clips: [...$("scene-editor").querySelectorAll("select")].map(select => Number(select.value)),
    brand: readBrand(), uploads: editingUploads, logo_key: $("brand-logo").value,
  };
}

async function openEditor(projectId) {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(projectId)) return;
  const requestId = ++editorRequest;
  $("editor").hidden = false;
  $("editor").scrollIntoView({ behavior: "smooth", block: "start" });
  editingProject = null;
  $("save-edit").disabled = true;
  $("scene-editor").replaceChildren();
  $("caption-editor").replaceChildren();
  $("export-edit").disabled = true;
  editMessage("Loading saved footage and captions...");
  try {
    const response = await fetch(`${R2_BASE}/projects/${projectId}/project.json`, { cache: "no-store" });
    if (!response.ok) throw Error("Editable source is unavailable for this Reel.");
    const project = await response.json();
    if (requestId !== editorRequest) return;
    editingProject = project;
    editingUploads = [];
    editingKey = projectId;
    let recipe = { words: project.timings.map(t => t.word), clips: project.segments.map(s => sources().findIndex(c => c.key === s.key)),
      logo_key: project.logo_key || "", brand: project.brand || { font: "Arial", color: "#ffffff", size: 76, position: 560 } };
    try {
      const saved = JSON.parse(localStorage.getItem(`reel-edit:${projectId}`));
      if (saved && Array.isArray(saved.words) && saved.words.length === recipe.words.length
          && saved.words.every(w => typeof w === "string" && w && w.length <= 40 && !/\s/.test(w))
          && Array.isArray(saved.clips) && saved.clips.length === recipe.clips.length
          && saved.clips.every(i => Number.isInteger(i) && i >= 0 && i < sources().length + (saved.uploads || []).length)
          && Array.isArray(saved.uploads || []) && (saved.uploads || []).length <= 8
          && validBrand(saved.brand)) recipe = saved;
    } catch { /* Local storage is optional. */ }
    editingUploads = recipe.uploads || [];
    $("scene-editor").replaceChildren();
    project.segments.forEach((scene, i) => {
      const card = document.createElement("div"); card.className = "scene-card";
      const label = document.createElement("label");
      label.textContent = `Scene ${i + 1} / ${scene.start.toFixed(1)}-${scene.end.toFixed(1)}s`;
      const video = document.createElement("video"); video.controls = true; video.muted = true;
      video.playsInline = true; video.preload = "metadata";
      const picture = document.createElement("img"); picture.alt = "Selected product image"; picture.style.width = "100%";
      const select = document.createElement("select"); select.setAttribute("aria-label", `Footage for scene ${i + 1}`);
      sources().forEach((candidate, j) => {
        const option = document.createElement("option"); option.value = j;
        option.textContent = `Clip ${j + 1}: ${candidate.label.slice(0, 55)}`;
        select.append(option);
      });
      select.value = recipe.clips[i];
      const preview = () => {
        const selected = sources()[Number(select.value)];
        video.hidden = selected.kind === "image";
        picture.hidden = selected.kind !== "image";
        picture.src = selected.kind === "image" ? `${R2_BASE}/${selected.key}` : "";
        video.src = selected.kind !== "image" ? `${R2_BASE}/${selected.key}` : "";
        video.poster = selected.poster_key ? `${R2_BASE}/${selected.poster_key}` : "";
      };
      select.addEventListener("change", preview); preview();
      card.append(label, video, picture, select); $("scene-editor").append(card);
    });
    $("caption-editor").replaceChildren();
    project.timings.forEach((timing, i) => {
      const label = document.createElement("label"); label.textContent = `${timing.start.toFixed(1)}s`;
      const input = document.createElement("input"); input.value = recipe.words[i]; input.maxLength = 40;
      input.setAttribute("aria-label", `Caption word ${i + 1}, ${timing.start.toFixed(1)} seconds`);
      label.append(input); $("caption-editor").append(label);
    });
    updateLogoOptions(recipe.logo_key || "");
    showBrand(recipe.brand);
    $("export-edit").disabled = Boolean(activeRun) || !exportReady;
    $("save-edit").disabled = false;
    editMessage("Ready to edit. Your original Reel stays unchanged.");
  } catch (error) { if (requestId === editorRequest) { editingProject = null; editMessage(error.message); } }
}

$("brand-settings").addEventListener("change", () => showBrand(readBrand()));
$("save-brand").addEventListener("click", () => {
  try { localStorage.setItem("reel-brand", JSON.stringify(readBrand())); editMessage("Brand preset saved in this browser."); }
  catch { editMessage("Browser storage is unavailable."); }
});
$("load-brand").addEventListener("click", () => {
  try {
    const brand = JSON.parse(localStorage.getItem("reel-brand"));
    if (brand) { showBrand(brand); editMessage("Brand preset applied to this edit."); }
    else editMessage("Save a brand preset first.");
  } catch { editMessage("Could not load the brand preset."); }
});
$("save-edit").addEventListener("click", () => {
  if (!editingProject) return;
  try { localStorage.setItem(`reel-edit:${editingKey}`, JSON.stringify(currentRecipe())); editMessage("Draft saved in this browser. No export started."); }
  catch { editMessage("Browser storage is unavailable."); }
});
$("export-edit").addEventListener("click", async () => {
  if (!editingProject || activeRun || !exportReady) return;
  const recipe = currentRecipe();
  if (recipe.words.some(w => !w || /\s/.test(w))) { editMessage("Use one word per timed caption field."); return; }
  $("export-edit").disabled = true;
  editMessage("Submitting your edit. No new AI generation will be used.");
  try {
    const response = await fetch("/api/revise", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ access_code: $("edit-access-code").value, project_id: editingKey, recipe }) });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "Export unavailable");
    startPolling(result.run_id);
    editMessage("Export started. Check progress above; preview the result before sharing.");
  } catch (error) { editMessage(error.message); }
  finally { $("export-edit").disabled = Boolean(activeRun) || !exportReady; }
});

$("upload-asset").addEventListener("click", async () => {
  if (!editingProject) return;
  const file = $("upload-media").files[0];
  if (!file || !["video/mp4", "image/png", "image/jpeg"].includes(file.type) || file.size > 20 * 1024 * 1024) {
    editMessage("Choose an MP4, PNG or JPEG under 20 MB."); return;
  }
  if (editingUploads.length >= 8) { editMessage("This edit already has eight uploads."); return; }
  const project = editingProject;
  $("upload-asset").disabled = true;
  editMessage("Uploading public demo media...");
  try {
    const response = await fetch("/api/assets", {method: "POST", headers: {
      "content-type": file.type, "x-access-code": $("edit-access-code").value }, body: file});
    const asset = await response.json();
    if (!response.ok) throw Error(asset.error || "Upload failed");
    if (project !== editingProject) return;
    asset.label = file.name.slice(0, 80);
    editingUploads.push(asset);
    updateLogoOptions($("brand-logo").value);
    for (const select of $("scene-editor").querySelectorAll("select")) {
      const option = document.createElement("option"); option.value = sources().length - 1;
      option.textContent = asset.label; select.append(option);
    }
    editMessage("Media added. Choose it from a scene's footage menu, then save your draft.");
  } catch (error) { editMessage(error.message); }
  finally { $("upload-asset").disabled = false; }
});

function updateLogoOptions(value) {
  const select = $("brand-logo"); select.replaceChildren();
  const none = document.createElement("option"); none.value = ""; none.textContent = "No logo"; select.append(none);
  for (const asset of sources().filter(a => a.kind === "image")) {
    const option = document.createElement("option"); option.value = asset.key; option.textContent = asset.label;
    select.append(option);
  }
  select.value = value;
}
