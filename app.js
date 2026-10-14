(() => {
  'use strict';

  const DEFAULT_API_BASE = 'https://youtube-mix.onrender.com';
  const API_KEY = 'studio-mix:api-url';
  const $ = (id) => document.getElementById(id);
  const apiUrl = $('apiUrl');
  const videoFile = $('videoFile');
  const audioFile = $('audioFile');
  const libraryList = $('libraryList');
  const generateButton = $('generate');
  const ytGenerateButton = $('ytGenerate');
  const toast = $('toast');

  // ── State ────────────────────────────────────────────────────────────────────
  let activeMode = 'local';         // 'local' | 'youtube'
  let activeSource = 'library';     // local audio source: 'library' | 'upload'
  let ytActiveAudio = 'url';        // youtube audio source: 'url' | 'library'
  let selectedAudioId = '';         // library audio id (shared between modes)
  let ytSelectedAudioId = '';       // library audio id for youtube mode
  let previewAudio = null;
  let currentObjectUrl = '';
  let toastTimer;
  let libraryItems = [];

  // ── Helpers ──────────────────────────────────────────────────────────────────

  function getApiUrl() {
    const custom = (localStorage.getItem(API_KEY) || '').trim();
    const value = custom || DEFAULT_API_BASE;
    return value.replace(/\/+$/, '');
  }

  function notify(message, error = false) {
    toast.textContent = message;
    toast.classList.toggle('error', error);
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  }

  async function request(path, options = {}) {
    const base = getApiUrl();
    if (!base) throw new Error("Adresse d'API introuvable.");
    let response;
    try {
      response = await fetch(`${base}${path}`, options);
    } catch (_) {
      throw new Error('API inaccessible. Si le backend Render etait en veille, il se reveille automatiquement (patientez ~30s et reessayez).');
    }
    if (!response.ok) {
      let detail = `Erreur HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (body.detail) detail = body.detail;
      } catch (_) { /* reponse non JSON */ }
      throw new Error(detail);
    }
    return response;
  }

  // ── Mode tabs (Local / YouTube) ───────────────────────────────────────────────

  function setMode(mode) {
    activeMode = mode;
    const isLocal = mode === 'local';
    $('tabModeLocal').classList.toggle('active', isLocal);
    $('tabModeYoutube').classList.toggle('active', !isLocal);
    $('tabModeLocal').setAttribute('aria-selected', String(isLocal));
    $('tabModeYoutube').setAttribute('aria-selected', String(!isLocal));
    $('localModePane').hidden = !isLocal;
    $('youtubeModePane').hidden = isLocal;
  }

  $('tabModeLocal').addEventListener('click', () => setMode('local'));
  $('tabModeYoutube').addEventListener('click', () => {
    setMode('youtube');
    loadYtLibrary();
  });

  // ── Local audio source tabs ──────────────────────────────────────────────────

  function setTab(tab) {
    activeSource = tab;
    const library = tab === 'library';
    $('tabLibrary').classList.toggle('active', library);
    $('tabUpload').classList.toggle('active', !library);
    $('tabLibrary').setAttribute('aria-selected', String(library));
    $('tabUpload').setAttribute('aria-selected', String(!library));
    $('libraryPane').hidden = !library;
    $('uploadPane').hidden = library;
  }

  $('tabLibrary').addEventListener('click', () => setTab('library'));
  $('tabUpload').addEventListener('click', () => setTab('upload'));

  // ── YouTube audio source tabs ─────────────────────────────────────────────────

  function setYtAudioTab(tab) {
    ytActiveAudio = tab;
    const isUrl = tab === 'url';
    $('ytTabUrlAudio').classList.toggle('active', isUrl);
    $('ytTabLibraryAudio').classList.toggle('active', !isUrl);
    $('ytTabUrlAudio').setAttribute('aria-selected', String(isUrl));
    $('ytTabLibraryAudio').setAttribute('aria-selected', String(!isUrl));
    $('ytUrlAudioPane').hidden = !isUrl;
    $('ytLibraryPane').hidden = isUrl;
  }

  $('ytTabUrlAudio').addEventListener('click', () => setYtAudioTab('url'));
  $('ytTabLibraryAudio').addEventListener('click', () => {
    setYtAudioTab('library');
    loadYtLibrary();
  });

  // Toggle "save audio" fields
  $('ytSaveAudio').addEventListener('change', () => {
    $('ytSaveAudioFields').hidden = !$('ytSaveAudio').checked;
  });

  // ── Volume controls ──────────────────────────────────────────────────────────

  $('volume').addEventListener('input', (event) => {
    const value = Number(event.target.value);
    $('volumeValue').value = `${value}%`;
    event.target.style.background = `linear-gradient(90deg,var(--accent) ${value / 2}%,#393a41 ${value / 2}%)`;
  });

  $('ytVolume').addEventListener('input', (event) => {
    const value = Number(event.target.value);
    $('ytVolumeValue').value = `${value}%`;
    event.target.style.background = `linear-gradient(90deg,var(--accent) ${value / 2}%,#393a41 ${value / 2}%)`;
  });

  // ── Library (shared helpers) ─────────────────────────────────────────────────

  /**
   * Build a track row and append it to a given container.
   * @param {Object} item  - library record
   * @param {string} listId - DOM id of the container
   * @param {string} currentId - currently selected audio id
   * @param {Function} onSelect - callback(id) when track is selected
   */
  function addTrack(item, listId, currentId, onSelect) {
    const container = $(listId);
    const row = document.createElement('div');
    row.className = `track${item.id === currentId ? ' selected' : ''}`;

    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'track-play';
    play.textContent = '▶';
    play.setAttribute('aria-label', `Praecouter ${item.title}`);
    play.addEventListener('click', async () => {
      try {
        if (previewAudio && !previewAudio.paused && previewAudio.dataset.id === item.id) {
          previewAudio.pause();
          play.textContent = '▶';
          return;
        }
        if (previewAudio) previewAudio.pause();
        const response = await request(`/library/${encodeURIComponent(item.id)}/audio`);
        const blob = await response.blob();
        if (currentObjectUrl) URL.revokeObjectURL(currentObjectUrl);
        currentObjectUrl = URL.createObjectURL(blob);
        previewAudio = new Audio(currentObjectUrl);
        // Reset all play buttons in ALL library lists
        document.querySelectorAll('.track-play').forEach((btn) => { btn.textContent = '▶'; });
        previewAudio.dataset.id = item.id;
        previewAudio.addEventListener('ended', () => { play.textContent = '▶'; });
        await previewAudio.play();
        play.textContent = 'II';
      } catch (error) {
        notify(error.message, true);
      }
    });

    const info = document.createElement('div');
    info.className = 'track-info';
    const title = document.createElement('strong');
    title.textContent = item.title || 'Sans titre';
    const meta = document.createElement('small');
    meta.textContent = formatDuration(item.duration_seconds);
    info.append(title, meta);

    const select = document.createElement('input');
    select.type = 'radio';
    select.name = `library-audio-${listId}`;
    select.checked = item.id === currentId;
    select.setAttribute('aria-label', `Utiliser ${item.title}`);
    select.addEventListener('change', () => {
      onSelect(item.id);
      container.querySelectorAll('.track').forEach((el) => el.classList.remove('selected'));
      row.classList.add('selected');
    });
    row.addEventListener('click', (event) => {
      if (event.target.closest('button')) return;
      select.checked = true;
      select.dispatchEvent(new Event('change'));
    });
    row.append(play, info, select);
    container.appendChild(row);
  }

  // ── Local library ────────────────────────────────────────────────────────────

  async function loadLibrary() {
    libraryList.replaceChildren();
    const loading = document.createElement('p');
    loading.className = 'empty';
    loading.textContent = 'Chargement de la bibliotheque...';
    libraryList.appendChild(loading);
    try {
      const response = await request('/library');
      libraryItems = await response.json();
      libraryList.replaceChildren();
      if (!libraryItems.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = 'Aucune piste pour le moment. Importez un fichier audio pour commencer.';
        libraryList.appendChild(empty);
        return;
      }
      if (!libraryItems.some((item) => item.id === selectedAudioId)) selectedAudioId = libraryItems[0].id;
      libraryItems.forEach((item) => addTrack(item, 'libraryList', selectedAudioId, (id) => { selectedAudioId = id; }));
    } catch (error) {
      libraryList.replaceChildren();
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = error.message;
      libraryList.appendChild(empty);
    }
  }

  // ── YouTube library ──────────────────────────────────────────────────────────

  async function loadYtLibrary() {
    const container = $('ytLibraryList');
    container.replaceChildren();
    const loading = document.createElement('p');
    loading.className = 'empty';
    loading.textContent = 'Chargement...';
    container.appendChild(loading);
    try {
      const response = await request('/library');
      const items = await response.json();
      container.replaceChildren();
      if (!items.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = 'Aucune piste. Importez via URL YouTube ou en mode fichiers locaux.';
        container.appendChild(empty);
        return;
      }
      if (!items.some((item) => item.id === ytSelectedAudioId)) ytSelectedAudioId = items[0].id;
      items.forEach((item) => addTrack(item, 'ytLibraryList', ytSelectedAudioId, (id) => { ytSelectedAudioId = id; }));
    } catch (error) {
      container.replaceChildren();
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = error.message;
      container.appendChild(empty);
    }
  }

  // ── Processing UI ────────────────────────────────────────────────────────────

  function showProcessing(loading, message = 'Le traitement peut prendre quelques minutes.') {
    $('progressPanel').hidden = !loading;
    $('progressTitle').textContent = loading ? 'Preparation du mix...' : 'Traitement termine';
    $('progressText').textContent = message;
    generateButton.disabled = loading;
    ytGenerateButton.disabled = loading;
    generateButton.querySelector('span:first-child').textContent = loading ? 'Traitement en cours...' : 'Generer & lire le mix';
    ytGenerateButton.querySelector('span:first-child').textContent = loading ? 'Traitement en cours...' : 'Generer via YouTube';
  }

  function displayResult(streamUrl, result, subtitle = '') {
    const fullUrl = `${getApiUrl()}${streamUrl}`;
    $('player').src = fullUrl;
    $('player').load();
    $('downloadLink').href = fullUrl;
    $('resultSubtitle').textContent = subtitle || 'Lecture optimisee pour les navigateurs mobiles.';
    $('expiryHint').textContent = `La video temporaire sera supprimee apres environ ${Math.max(1, Math.round(result.expires_in_seconds / 60))} min.`;
    $('resultPanel').hidden = false;
    showProcessing(false);
    $('resultPanel').scrollIntoView({ behavior: 'smooth', block: 'center' });
    try { $('player').play(); } catch (_) { /* Safari peut exiger une action explicite */ }
  }

  // ── Local mode: prepare audio ─────────────────────────────────────────────────

  async function prepareAudio() {
    if (activeSource === 'library') {
      if (!selectedAudioId) throw new Error('Selectionnez une piste audio dans la bibliotheque.');
      return { audio_id: selectedAudioId };
    }
    const file = audioFile.files[0];
    if (!file) throw new Error('Choisissez un fichier audio a importer.');
    const form = new FormData();
    form.append('file', file, file.name);
    const title = $('audioTitle').value.trim();
    if (title) form.append('title', title);
    const response = await request('/library', { method: 'POST', body: form });
    const item = await response.json();
    selectedAudioId = item.id;
    await loadLibrary();
    return { audio_id: item.id };
  }

  // ── Local mode: generate ─────────────────────────────────────────────────────

  async function generate() {
    const video = videoFile.files[0];
    if (!video) {
      notify('Choisissez un fichier video avant de lancer le mix.', true);
      return;
    }
    try {
      showProcessing(true, 'Envoi des fichiers puis rendu du MP4. Gardez cette page ouverte.');
      $('resultPanel').hidden = true;
      const audio = await prepareAudio();
      const form = new FormData();
      form.append('video', video, video.name);
      form.append('audio_id', audio.audio_id);
      form.append('volume', String(Number($('volume').value) / 100));
      const response = await request('/process', { method: 'POST', body: form });
      const result = await response.json();
      displayResult(result.stream_url, result);
    } catch (error) {
      showProcessing(false);
      notify(error.message, true);
    }
  }

  // ── YouTube mode: generate ────────────────────────────────────────────────────

  async function ytGenerate() {
    const videoUrl = $('ytVideoUrl').value.trim();
    if (!videoUrl) {
      notify('Saisissez l\'URL de la video YouTube.', true);
      return;
    }

    try {
      showProcessing(true, 'Extraction des flux YouTube puis rendu du MP4. Cela peut prendre quelques minutes...');
      $('resultPanel').hidden = true;

      const volume = String(Number($('ytVolume').value) / 100);

      // If user wants to save the audio to library first
      if (ytActiveAudio === 'url' && $('ytSaveAudio').checked) {
        const audioUrl = $('ytAudioUrl').value.trim();
        if (!audioUrl) throw new Error('Saisissez l\'URL audio YouTube a sauvegarder.');
        const saveForm = new FormData();
        saveForm.append('audio_url', audioUrl);
        const savedTitle = $('ytAudioTitle').value.trim();
        if (savedTitle) saveForm.append('title', savedTitle);
        showProcessing(true, 'Import de l\'audio YouTube dans la bibliotheque...');
        const importResp = await request('/youtube/import-audio', { method: 'POST', body: saveForm });
        const importedTrack = await importResp.json();
        // Reload libraries so the new track appears
        await Promise.all([loadLibrary(), loadYtLibrary()]);
        notify(`Audio "${importedTrack.title}" ajoute a la bibliotheque.`);
        // Now launch process using the saved audio_id
        const body = { video_url: videoUrl, audio_url: '', volume: parseFloat(volume), audio_id: importedTrack.id };
        showProcessing(true, 'Mixage video + audio en cours...');
        const procResp = await request('/youtube/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const result = await procResp.json();
        displayResult(result.stream_url, result, result.video_title ? `"${result.video_title}"` : '');
        return;
      }

      // Build request body
      const body = {
        video_url: videoUrl,
        volume: parseFloat(volume),
      };

      if (ytActiveAudio === 'library') {
        if (!ytSelectedAudioId) throw new Error('Selectionnez une piste dans la bibliotheque.');
        body.audio_id = ytSelectedAudioId;
        body.audio_url = '';
      } else {
        const audioUrl = $('ytAudioUrl').value.trim();
        if (!audioUrl) throw new Error('Saisissez l\'URL audio YouTube.');
        body.audio_url = audioUrl;
      }

      const response = await request('/youtube/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      displayResult(result.stream_url, result, result.video_title ? `"${result.video_title}"` : '');
    } catch (error) {
      showProcessing(false);
      notify(error.message, true);
    }
  }

  // ── Event listeners ──────────────────────────────────────────────────────────

  $('refreshLibrary').addEventListener('click', loadLibrary);
  $('ytRefreshLibrary').addEventListener('click', loadYtLibrary);

  $('settingsToggle').addEventListener('click', () => {
    $('settingsPanel').hidden = !$('settingsPanel').hidden;
    apiUrl.value = getApiUrl();
    if (!$('settingsPanel').hidden) apiUrl.focus();
  });

  $('saveApi').addEventListener('click', () => {
    const value = apiUrl.value.trim().replace(/\/+$/, '');
    if (value && !/^https?:\/\//i.test(value)) {
      notify("L'URL doit commencer par https:// ou http://.", true);
      return;
    }
    try {
      if (value && value !== DEFAULT_API_BASE) {
        localStorage.setItem(API_KEY, value);
      } else {
        localStorage.removeItem(API_KEY);
      }
    } catch (_) {
      notify('Le stockage local est indisponible dans ce navigateur.', true);
      return;
    }
    $('settingsPanel').hidden = true;
    notify("Adresse active : " + getApiUrl());
    loadLibrary();
  });

  videoFile.addEventListener('change', () => { $('videoName').textContent = videoFile.files[0]?.name || 'Parcourir les fichiers'; });
  audioFile.addEventListener('change', () => { $('audioName').textContent = audioFile.files[0]?.name || 'Choisir un fichier audio'; });

  generateButton.addEventListener('click', generate);
  ytGenerateButton.addEventListener('click', ytGenerate);

  window.addEventListener('beforeunload', () => {
    if (previewAudio) previewAudio.pause();
    if (currentObjectUrl) URL.revokeObjectURL(currentObjectUrl);
  });

  // ── Init ──────────────────────────────────────────────────────────────────────
  loadLibrary();
})();
