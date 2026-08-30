
(function () {
  // Substituted at render time from the ICON_* constants, so the markup
  // and the JS that swaps it can never drift out of sync.
  var ICON_PLAY = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
  var ICON_PAUSE = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" focusable="false"><path fill="currentColor" d="M6 5h3.5v14H6zM14.5 5H18v14h-3.5z"/></svg>';
  var ICON_PREV = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false"><path fill="currentColor" d="M6 6h2.5v12H6zm3.5 6L18 6v12z"/></svg>';
  var ICON_NEXT = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false"><path fill="currentColor" d="M15.5 6H18v12h-2.5zM6 6l8.5 6L6 18z"/></svg>';

  var ytPlayer = null, ytReady = false, ytPendingInit = null;
  var audioEl = new Audio();
  audioEl.preload = 'none';

  var activeEngine = null;  // 'yt' | 'audio' | null
  var activeBar = null, activeVideoId = null, activeSrc = null;
  var pendingSeekFraction = null;
  var pollTimer = null;

  function fmt(t) {
    if (!isFinite(t) || t < 0) t = 0;
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function row(bar) { return bar.closest('.trow'); }

  function paint(bar, fraction, curLabel, durLabel, state) {
    var fillEl = bar.querySelector('.fill');
    if (fillEl) fillEl.style.width = (Math.max(0, Math.min(1, fraction)) * 100) + '%';
    bar.setAttribute('aria-valuenow', Math.round(fraction * 100));
    var r = row(bar);
    if (!r) return;
    r.classList.toggle('playing', state === 'playing');
    var t = r.querySelector('.time');
    if (t) t.textContent = curLabel + ' / ' + durLabel;
    var btn = r.querySelector('.ppbtn');
    if (btn) {
      btn.classList.toggle('loading', state === 'loading');
      btn.innerHTML = state === 'playing' ? ICON_PAUSE : ICON_PLAY;
    }
    // Keep the bottom transport in step with the row: same play/pause icon,
    // same progress, same clock.
    var nbToggle = document.querySelector('#nowbar [data-toggle]');
    if (nbToggle) {
      nbToggle.innerHTML = state === 'playing' ? ICON_PAUSE : ICON_PLAY;
    }
    if (bar === activeBar) {
      var nbFill = document.querySelector('#nowbar .nbfill');
      if (nbFill) nbFill.style.width = (Math.max(0, Math.min(1, fraction)) * 100) + '%';
      var nbSeek = document.querySelector('#nowbar .nbseek');
      if (nbSeek) nbSeek.setAttribute('aria-valuenow', Math.round(fraction * 100));
      var nbTime = document.querySelector('#nowbar .nbtime');
      if (nbTime) nbTime.textContent = curLabel + ' / ' + durLabel;
    }
  }

  function knownDuration(bar) {
    return parseInt(bar.getAttribute('data-dur'), 10) || 0;
  }

  function durLabel(bar) {
    var known = knownDuration(bar);
    return known ? fmt(known) : '--:--';
  }

  function resetBar(bar) {
    if (!bar) return;
    paint(bar, 0, '0:00', durLabel(bar), 'idle');
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---- YouTube backend (Discogs releases) ----

  function pollYt() {
    if (!ytPlayer || !activeBar) return;
    var dur = 0, cur = 0, state = -1;
    try {
      dur = ytPlayer.getDuration() || 0;
      cur = ytPlayer.getCurrentTime() || 0;
      state = ytPlayer.getPlayerState();
    } catch (err) { return; }
    if (pendingSeekFraction !== null && dur > 0) {
      var target = pendingSeekFraction; pendingSeekFraction = null;
      ytPlayer.seekTo(target * dur, true);
      cur = target * dur;
    }
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar),
          state === 1 ? 'playing' : (state === 3 ? 'loading' : 'paused'));
  }

  function ensureYt(cb) {
    if (ytPlayer) { cb(); return; }
    if (!ytReady) { ytPendingInit = cb; return; }
    var host = document.getElementById('yt-audio-host');
    if (!host) return;
    ytPlayer = new YT.Player(host, {
      height: '113', width: '200',
      playerVars: { playsinline: 1, controls: 0, disablekb: 1, rel: 0, modestbranding: 1 },
      events: {
        onReady: function () { cb(); },
        onStateChange: function (ev) {
          if (ev.data === YT.PlayerState.ENDED && activeEngine === 'yt' && activeBar) {
            var d = ytPlayer.getDuration() || knownDuration(activeBar);
            paint(activeBar, 1, fmt(d), fmt(d), 'paused');
            stopPolling();
            step(1);  // roll on to the next track, top to bottom
          }
        },
        onError: function () {
          if (activeEngine === 'yt' && activeBar) {
            var r = row(activeBar);
            if (r) r.classList.add('errored');
          }
        }
      }
    });
  }

  window.onYouTubeIframeAPIReady = function () {
    ytReady = true;
    if (ytPendingInit) { var cb = ytPendingInit; ytPendingInit = null; cb(); }
  };

  function playYtFrom(bar, fraction) {
    var videoId = bar.getAttribute('data-yt');
    ensureYt(function () {
      if (activeBar && activeBar !== bar) resetBar(activeBar);
      if (activeEngine !== 'yt' || activeVideoId !== videoId) {
        activeEngine = 'yt'; activeVideoId = videoId; activeSrc = null; activeBar = bar;
        var known = knownDuration(bar);
        pendingSeekFraction = fraction > 0.01 ? fraction : null;
        ytPlayer.loadVideoById(videoId);
        // Best-effort immediate jump using Discogs' own track length, so a
        // click deep into a bar does not sit at 0:00 waiting for YouTube's
        // own metadata to arrive. The poll loop re-seeks once the real
        // duration is confirmed, in case this fires before load is ready.
        if (known > 0 && pendingSeekFraction !== null) {
          try { ytPlayer.seekTo(pendingSeekFraction * known, true); } catch (err) {}
        }
      } else {
        activeBar = bar;
        var dur = ytPlayer.getDuration() || knownDuration(bar);
        if (dur > 0) ytPlayer.seekTo(fraction * dur, true);
        ytPlayer.playVideo();
      }
      stopPolling();
      pollTimer = setInterval(pollYt, 250);
      paint(bar, fraction, fmt(fraction * (ytPlayer.getDuration() || knownDuration(bar))),
            durLabel(bar), 'loading');
    });
  }

  // ---- direct-audio backend (clone.nl, or any source with real MP3s) ----

  function pollAudio() {
    if (!activeBar) return;
    var dur = audioEl.duration || 0;
    var cur = audioEl.currentTime || 0;
    var effectiveDur = dur || knownDuration(activeBar);
    var frac = effectiveDur > 0 ? cur / effectiveDur : 0;
    var state = !audioEl.paused && !audioEl.ended ? 'playing'
               : (audioEl.readyState < 2 ? 'loading' : 'paused');
    paint(activeBar, frac, fmt(cur), dur > 0 ? fmt(dur) : durLabel(activeBar), state);
  }

  audioEl.addEventListener('ended', function () {
    if (activeEngine === 'audio' && activeBar) {
      var d = audioEl.duration || knownDuration(activeBar);
      paint(activeBar, 1, fmt(d), fmt(d), 'paused');
      stopPolling();
      step(1);  // roll on to the next track, top to bottom
    }
  });
  audioEl.addEventListener('error', function () {
    if (activeEngine === 'audio' && activeBar) {
      var r = row(activeBar);
      if (r) r.classList.add('errored');
    }
  });

  function safePlay() {
    var p = audioEl.play();
    if (p && p.catch) p.catch(function () {});  // ignore benign AbortError on rapid re-clicks
  }

  function playAudioFrom(bar, fraction) {
    var src = bar.getAttribute('data-src');
    if (activeBar && activeBar !== bar) resetBar(activeBar);
    if (activeEngine !== 'audio' || activeSrc !== src) {
      activeEngine = 'audio'; activeSrc = src; activeVideoId = null; activeBar = bar;
      audioEl.src = src;
      if (fraction > 0.01) {
        var onMeta = function () {
          audioEl.currentTime = fraction * (audioEl.duration || 0);
          audioEl.removeEventListener('loadedmetadata', onMeta);
        };
        audioEl.addEventListener('loadedmetadata', onMeta);
      }
      safePlay();
    } else {
      activeBar = bar;
      if (audioEl.duration) audioEl.currentTime = fraction * audioEl.duration;
      safePlay();
    }
    stopPolling();
    pollTimer = setInterval(pollAudio, 200);
    paint(bar, fraction, fmt(fraction * (audioEl.duration || knownDuration(bar))),
          durLabel(bar), 'loading');
  }

  // ---- track navigation: walk the page top to bottom ----

  // Rebuilt on each call rather than cached: cheap at this page size, and
  // it stays correct if a row gets marked .errored mid-session (a dead
  // YouTube video or a 404 preview), which should then be skipped over
  // rather than stopping playback dead when auto-advancing.
  function playableBars() {
    var out = [];
    var all = document.querySelectorAll('.bar');
    for (var i = 0; i < all.length; i++) {
      var r = row(all[i]);
      if (!r || !r.classList.contains('errored')) out.push(all[i]);
    }
    return out;
  }

  function step(delta) {
    var bars = playableBars();
    if (!bars.length) return;
    var idx = -1;
    for (var i = 0; i < bars.length; i++) {
      if (bars[i] === activeBar) { idx = i; break; }
    }
    var next = idx === -1 ? (delta > 0 ? 0 : bars.length - 1) : idx + delta;
    if (next < 0 || next >= bars.length) return;  // stop at the ends, don't wrap
    var target = bars[next];
    playFrom(target, 0);
    if (target.scrollIntoView) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    try { target.focus({ preventScroll: true }); } catch (err) { target.focus(); }
  }

  // ---- floating transport ----

  // The generated digest pages ship this markup in their HTML; likes.html
  // does not, and building it here rather than hand-copying it there keeps
  // the transport defined in exactly one place. Both call sites end up with
  // the same controls, and adding a third page costs nothing.
  function ensureChrome() {
    if (!document.body) return;
    if (!document.getElementById('yt-audio-host')) {
      var host = document.createElement('div');
      host.style.cssText =
        'position:fixed;left:-9999px;top:0;width:200px;height:113px;';
      host.innerHTML = '<div id="yt-audio-host"></div>';
      document.body.appendChild(host);
    }
    if (!document.getElementById('nowbar')) {
      var nb = document.createElement('div');
      nb.id = 'nowbar';
      nb.innerHTML =
        '<div class="nbseek" role="slider" aria-valuemin="0" aria-valuemax="100" ' +
        'aria-valuenow="0" aria-label="Seek in current track">' +
        '<div class="nbtrack"><div class="nbfill"></div></div></div>' +
        '<div class="nbrow"><div class="nbctrls">' +
        '<button type="button" data-step="-1" title="Previous track (p)" ' +
        'aria-label="Previous track">' + ICON_PREV + '</button>' +
        '<button type="button" data-toggle="1" title="Play / pause (space)" ' +
        'aria-label="Play or pause">' + ICON_PAUSE + '</button>' +
        '<button type="button" data-step="1" title="Next track (n)" ' +
        'aria-label="Next track">' + ICON_NEXT + '</button>' +
        '</div><span class="nowtitle"></span>' +
        '<span class="nbtime"></span></div>';
      document.body.appendChild(nb);
    }
  }

  ensureChrome();
  var nowbar = document.getElementById('nowbar');
  var nowtitle = nowbar ? nowbar.querySelector('.nowtitle') : null;

  function updateNowBar(bar) {
    if (!nowbar) return;
    if (!bar) { nowbar.classList.remove('show'); return; }
    nowbar.classList.add('show');
    if (nowtitle) {
      var li = bar.closest ? bar.closest('li.track') : null;
      var name = li ? li.querySelector('.tname') : null;
      nowtitle.textContent = name ? name.textContent : (bar.getAttribute('aria-label') || '');
    }
  }

  if (nowbar) {
    nowbar.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button') : null;
      if (btn) {
        ev.preventDefault();
        if (btn.getAttribute('data-toggle')) {
          if (activeBar) togglePlayPause(activeBar);
        } else {
          step(parseInt(btn.getAttribute('data-step'), 10) || 1);
        }
        return;
      }
      // Seeking from the bottom bar acts on whatever is currently playing,
      // so you can scrub without scrolling back to that record's row.
      var seek = ev.target.closest ? ev.target.closest('.nbseek') : null;
      if (seek && activeBar) {
        ev.preventDefault();
        playFrom(activeBar, fractionFromEvent(seek, ev));
      }
    });
  }

  // ---- unified dispatch: only one backend plays at a time ----

  function playFrom(bar, fraction) {
    var isAudio = !!bar.getAttribute('data-src');
    if (isAudio) {
      if (activeEngine === 'yt' && ytPlayer) { try { ytPlayer.pauseVideo(); } catch (err) {} }
      playAudioFrom(bar, fraction);
    } else {
      if (activeEngine === 'audio') { audioEl.pause(); }
      playYtFrom(bar, fraction);
    }
    updateNowBar(bar);
  }

  function togglePlayPause(bar) {
    var isAudio = !!bar.getAttribute('data-src');
    var isActiveTrack = isAudio
      ? (activeEngine === 'audio' && activeSrc === bar.getAttribute('data-src'))
      : (activeEngine === 'yt' && activeVideoId === bar.getAttribute('data-yt'));
    if (!isActiveTrack) { playFrom(bar, 0); return; }
    if (isAudio) {
      if (audioEl.paused) { safePlay(); } else { audioEl.pause(); }
    } else if (ytPlayer) {
      var state = ytPlayer.getPlayerState();
      if (state === 1) { ytPlayer.pauseVideo(); } else { ytPlayer.playVideo(); }
    }
  }

  function fractionFromEvent(bar, ev) {
    var rect = bar.getBoundingClientRect();
    var x = (ev.clientX !== undefined && ev.clientX !== 0) ? ev.clientX
          : (ev.changedTouches && ev.changedTouches[0] ? ev.changedTouches[0].clientX : rect.left);
    return Math.min(1, Math.max(0, (x - rect.left) / rect.width));
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('button.ppbtn') : null;
    if (btn) {
      ev.preventDefault();
      var pbar = row(btn).querySelector('.bar');
      if (pbar) togglePlayPause(pbar);
      return;
    }
    if (ev.target.closest && ev.target.closest('a.ytlink')) return; // let it navigate
    var bar = ev.target.closest ? ev.target.closest('.bar') : null;
    if (bar) {
      ev.preventDefault();
      playFrom(bar, fractionFromEvent(bar, ev));
    }
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    var t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;

    // Next / previous work from anywhere on the page, so you can skip
    // through without first clicking a row to focus it. Deliberately NOT
    // bound to bare Up/Down arrows at this level -- those still need to
    // scroll the page normally.
    var k = ev.key.toLowerCase();
    if (k === 'n' || k === 'j' || ev.key === 'MediaTrackNext') {
      ev.preventDefault(); step(1); return;
    }
    if (k === 'p' || k === 'k' || ev.key === 'MediaTrackPrevious') {
      ev.preventDefault(); step(-1); return;
    }

    var el = document.activeElement;
    if (!el || !el.classList || !el.classList.contains('bar')) return;
    if (ev.key === ' ' || ev.key === 'Enter') {
      ev.preventDefault();
      togglePlayPause(el);
    } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      // Only once a bar has focus, where arrow keys clearly belong to the
      // player rather than to scrolling the page.
      ev.preventDefault();
      step(ev.key === 'ArrowDown' ? 1 : -1);
    } else if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
      ev.preventDefault();
      if (activeBar !== el) return;
      var delta = ev.key === 'ArrowRight' ? 5 : -5;
      if (activeEngine === 'audio') {
        audioEl.currentTime = Math.min(audioEl.duration || 1e9, Math.max(0, audioEl.currentTime + delta));
      } else if (activeEngine === 'yt' && ytPlayer) {
        var dur = ytPlayer.getDuration() || knownDuration(el);
        var cur = ytPlayer.getCurrentTime() || 0;
        ytPlayer.seekTo(Math.min(dur, Math.max(0, cur + delta)), true);
      }
    }
  });

  // Exported so likes.html can build identical track rows from the data
  // stored in likes.json. One definition of the markup, several pages --
  // a second hand-written copy over there would drift the moment either
  // the structure or the icons change. The click/keyboard handlers above
  // are delegated on document, so rows added later still work.
  window.DigestPlayer = {
    trackRowHtml: function (t) {
      var esc = function (s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      };
      var title = String(t.title || 'Listen');
      var shown = title.length > 70 ? title.slice(0, 69).replace(/\s+$/, '') + '\u2026' : title;
      var dur = parseInt(t.dur, 10) || 0;
      var ytAttr = t.yt ? ' data-yt="' + esc(t.yt) + '"' : '';
      var srcAttr = t.src ? ' data-src="' + esc(t.src) + '"' : '';
      var link = t.yt
        ? '<a class="ytlink" href="https://www.youtube.com/watch?v=' + esc(t.yt) +
          '" target="_blank" rel="noopener noreferrer" title="Watch on YouTube" ' +
          'aria-label="Watch on YouTube">\u2197</a>'
        : '';
      return '<li class="track"><div class="trow">' +
        '<button class="ppbtn" aria-label="Play ' + esc(shown) + '">' + ICON_PLAY + '</button>' +
        '<div class="bar" tabindex="0" role="slider" aria-valuemin="0" ' +
        'aria-valuemax="100" aria-valuenow="0" aria-label="' + esc(title) + '"' +
        ytAttr + srcAttr + ' data-dur="' + dur + '">' +
        '<div class="track"><div class="fill"></div></div></div>' +
        '<span class="time">0:00 / ' + (dur ? fmt(dur) : '--:--') + '</span>' +
        link + '</div><div class="tname">' + esc(shown) + '</div></li>';
    }
  };

  var tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
})();
