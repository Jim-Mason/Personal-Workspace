/* Personal Workspace 前端 —— 无构建、无框架、无外部依赖。
   改完直接刷新浏览器即可，不需要编译。 */
(function () {
  'use strict';

  var VIEW_KEY = 'pw_view_mode';

  // 模块一（本机资产清单）已经不再是内核的一部分，它住在 modules/inventory/，
  // 由中台挂到 /inventory 之下。放在这里当常量：模块的 URL 前缀只该出现一次，
  // 散在四处的话，哪天模块换个挂载点就会漏改一两处 —— 而漏改的表现是
  // 某个按钮悄悄 401，不是报错。
  var API_APPS = '/inventory/api/apps';

  var state = {
    apps: [],
    overview: null,
    events: [],
    modules: [],
    moduleErrors: {},
    branding: null,
    draft: null,
    busy: false,
    tab: 'overview',
    // 正在 iframe 里跑着的模块；null 表示没开
    openModuleId: null,
    // ?module=<id> 深链接的待兑现意图（要等模块列表回来才知道 open_url）
    pendingModule: null,
    logModuleId: null,
    moduleTimer: null,
    view: localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'grid'
  };

  /* ------------------------------------------------------------ 令牌与会话 */
  // 令牌只从地址栏读一次 —— 入口地址（start.bat 打印的那条）里带着它。
  // **刻意不写进 localStorage / sessionStorage**：换到会话之后就不需要它了，
  // 留着只是多一份明文凭据躺在浏览器里。地址栏里的痕迹也当场抹掉。
  function readEntryToken() {
    var params = new URLSearchParams(location.search);
    var fromUrl = params.get('token') || '';
    if (fromUrl) {
      params.delete('token');
      var qs = params.toString();
      history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
    }
    return fromUrl;
  }

  // 这次打开手里握着的令牌：可能是入口地址给的，也可能是登录门上刚粘的。
  // 会话建立之后它就没用了，但留着无害 —— 万一 Cookie 被浏览器清掉，
  // 同一个页面还能凭它再换一次会话，不用重新找。
  var TOKEN = readEntryToken();

  // 用户很可能把整条访问地址直接粘进来，所以要先从里面把令牌认出来。
  function extractToken(text) {
    var value = String(text || '').trim();
    if (!value) return '';
    var match = value.match(/[?&]token=([^&\s]+)/);
    return match ? decodeURIComponent(match[1]) : value;
  }

  /* ------------------------------------------------------------ 登录门 */
  // 会话失效时把门推出来。**为什么不是留一条常驻红字**：接口 401 时页面外壳
  // 照常渲染、只是数据全空，看着像"模块坏了"；而且红字只能看、不能做任何事。
  // 门推出来，输一次就恢复了 —— 这也是它替换掉原来那块提示的原因。
  var loginRequired = false;

  function setLoginError(message) {
    var box = $('login-error');
    if (!box) return;
    box.textContent = message || '';
    box.hidden = !message;
  }

  function showLoginGate(message) {
    var gate = $('login-gate');
    if (!gate) return;
    if (!loginRequired) {
      loginRequired = true;
      // 停掉模块轮询：会话都没了，它每几秒试一次只会把日志刷满
      if (state.moduleTimer) {
        clearInterval(state.moduleTimer);
        state.moduleTimer = null;
      }
      var input = $('login-token');
      if (input) {
        input.value = '';
        input.focus();
      }
    }
    gate.hidden = false;
    // 只有调用方明确给了原因才覆盖 —— 否则会把用户输错时的提示冲掉
    if (message) setLoginError(message);
  }

  function hideLoginGate() {
    loginRequired = false;
    var gate = $('login-gate');
    if (gate) gate.hidden = true;
    setLoginError('');
  }

  /* ------------------------------------------------------------ 会话接口 */
  function checkSession() {
    return fetch('/api/session', { cache: 'no-store' }).then(function (res) {
      return res.json().catch(function () {
        return {};
      });
    });
  }

  function submitSession(token) {
    return fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    }).then(function (res) {
      return res
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!res.ok) throw new Error((data && data.detail) || '令牌不对');
          return data;
        });
    });
  }

  function logout() {
    // 先请求再刷新：即使请求失败也照样刷新，本地 Cookie 由服务端的
    // Set-Cookie 清掉；万一服务没起来，刷新至少让用户回到登录门。
    fetch('/api/session', { method: 'DELETE' })
      .catch(function () {})
      .then(function () {
        TOKEN = '';
        location.reload();
      });
  }

  /* ------------------------------------------------------------ 请求 */
  function api(path, options) {
    var opts = options || {};
    var headers = Object.assign({}, opts.headers || {});
    // 会话正常时压根用不上它；带上只是为了「Cookie 被清掉」这种场景
    // 能在同一次打开里自愈。有就带，没有就不带。
    if (TOKEN) headers['X-LocalDeck-Token'] = TOKEN;
    if (opts.body) headers['Content-Type'] = 'application/json';
    return fetch(path, Object.assign({}, opts, { headers: headers })).then(function (res) {
      return res
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!res.ok) {
            // 401 = 没进门。这不止是"这一次请求失败"，后面每个接口都会失败，
            // 所以直接把登录门推出来，而不是让调用方弹一条 toast 就走。
            // 登录接口自己的 401 要放过 —— 那时门已经开着，服务端给的
            // 「令牌不对」比这里的通用文案准确得多。
            if (res.status === 401 && path.indexOf('/api/session') !== 0) {
              showLoginGate();
            }
            throw new Error((data && data.detail) || '请求失败（HTTP ' + res.status + '）');
          }
          return data;
        });
    });
  }

  /* ------------------------------------------------------------ 工具 */
  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function formatBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n <= 0) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var value = n;
    var i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i += 1;
    }
    return (value >= 100 ? Math.round(value) : value.toFixed(1)) + ' ' + units[i];
  }

  function formatKb(bytes) {
    return Math.round(bytes / 1024) + ' KB';
  }

  function initials(name) {
    var text = String(name || '?').trim();
    if (!text) return '?';
    if (/[\u4e00-\u9fa5]/.test(text)) return text.slice(0, 1);
    var parts = text.split(/[\s\-_.]+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return text.slice(0, 2).toUpperCase();
  }

  // 按名字算一个稳定的配色位置，让图标在「主色 ↔ 副色」之间散开。
  // 用 color-mix 而不是写死一串色号：用户换主色时整片图标跟着换。
  function hashCode(text) {
    var h = 0;
    var s = String(text || '');
    for (var i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h);
  }

  function iconStyle(name) {
    var p = hashCode(name) % 101;
    var q = (p + 38) % 101;
    return (
      'background:linear-gradient(135deg,' +
      'color-mix(in srgb, var(--accent) ' + p + '%, var(--accent-2)),' +
      'color-mix(in srgb, var(--accent) ' + q + '%, var(--accent-2)))'
    );
  }

  var toastTimer = null;
  function toast(message, isError) {
    // 登录门已经说明情况了，这里别再插一条 3 秒就消失的红条 ——
    // 模块轮询每几秒触发一次，连弹只会让人以为"一直在坏、不知道怎么办"。
    if (isError && loginRequired) return;
    var el = $('toast');
    el.textContent = message;
    el.classList.toggle('is-error', !!isError);
    el.hidden = false;
    // 重放入场动画：连续两次提示时，不会因为元素已在而毫无反馈
    el.style.animation = 'none';
    void el.offsetWidth;
    el.style.animation = '';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(
      function () {
        el.hidden = true;
      },
      isError ? 6500 : 3600
    );
  }

  function setBusy(busy, label) {
    state.busy = busy;
    ['btn-scan', 'btn-scan-hero'].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.disabled = busy;
      if (id === 'btn-scan') el.textContent = busy ? label || '处理中…' : '扫描本机';
      else el.textContent = busy ? label || '处理中…' : '开始扫描';
    });
  }

  /* ============================================================ 品牌与外观 */

  function clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  var WRITABLE = [
    'app_name',
    'tagline',
    'logo_text',
    'logo_image',
    'mode',
    'preset',
    'accent',
    'accent2',
    'bg',
    'surface',
    'text',
    'bg_image',
    'bg_overlay',
    'bg_style'
  ];

  function pickWritable(source) {
    var out = {};
    WRITABLE.forEach(function (key) {
      if (source && source[key] !== undefined) out[key] = source[key];
    });
    return out;
  }

  /** 把一份品牌配置落到页面上：CSS 变量 + 标题 + 品牌文字。 */
  function applyBranding(b) {
    if (!b) return;
    var root = document.documentElement;
    root.style.setProperty('--accent', b.accent);
    root.style.setProperty('--accent-2', b.accent2);
    root.style.setProperty('--bg', b.bg);
    root.style.setProperty('--surface', b.surface);
    root.style.setProperty('--text', b.text);
    root.style.setProperty('--bg-overlay', (Number(b.bg_overlay) / 100).toFixed(2));
    root.style.setProperty('--bg-image', b.bg_image ? 'url("' + b.bg_image + '")' : 'none');
    root.style.setProperty('--brand-logo', b.logo_image ? 'url("' + b.logo_image + '")' : 'none');
    root.dataset.mode = b.mode;
    root.dataset.bg = b.bg_style;

    document.title = b.app_name || 'Personal Workspace';
    $('brand-name').textContent = b.app_name || 'Personal Workspace';
    $('brand-tagline').textContent = b.tagline || '';
    var logoText = $('brand-logo-text');
    logoText.textContent = b.logo_text || '';
    // 有 Logo 图就藏掉文字，否则白字压在图上大概率看不清
    logoText.style.display = b.logo_image ? 'none' : '';
    $('brand-tagline').hidden = !b.tagline;
  }

  function renderPresets() {
    var presets = (state.branding && state.branding.presets) || {};
    $('preset-row').innerHTML = Object.keys(presets)
      .map(function (key) {
        var p = presets[key];
        return (
          '<button type="button" class="preset" data-preset="' + escapeHtml(key) + '">' +
          '<i style="background:linear-gradient(135deg,' + escapeHtml(p.accent) + ',' + escapeHtml(p.accent2) + ')"></i>' +
          escapeHtml(p.label) +
          '</button>'
        );
      })
      .join('');
  }

  function syncPresetHighlight() {
    var current = state.draft ? state.draft.preset : '';
    document.querySelectorAll('.preset').forEach(function (el) {
      el.classList.toggle('is-active', el.dataset.preset === current);
    });
  }

  function syncSegs() {
    var d = state.draft;
    if (!d) return;
    document.querySelectorAll('#mode-switch button').forEach(function (el) {
      el.classList.toggle('is-active', el.dataset.mode === d.mode);
    });
    document.querySelectorAll('#bgstyle-switch button').forEach(function (el) {
      el.classList.toggle('is-active', el.dataset.bg === d.bg_style);
    });
  }

  function setThumb(el, image, fallback) {
    if (image) {
      el.style.backgroundImage = 'url("' + image + '")';
      el.innerHTML = '';
    } else {
      el.style.backgroundImage = '';
      el.innerHTML = '<span>' + escapeHtml(fallback) + '</span>';
    }
  }

  function fillForm(d) {
    $('f-app-name').value = d.app_name || '';
    $('f-tagline').value = d.tagline || '';
    $('f-logo-text').value = d.logo_text || '';
    $('f-accent').value = d.accent;
    $('f-accent2').value = d.accent2;
    $('f-bg').value = d.bg;
    $('f-surface').value = d.surface;
    $('f-text').value = d.text;
    $('f-overlay').value = d.bg_overlay;
    $('overlay-value').textContent = d.bg_overlay;
    setThumb($('logo-thumb'), d.logo_image, '无');
    setThumb($('bg-thumb'), d.bg_image, '无');
    syncPresetHighlight();
    syncSegs();
    $('upload-hint').textContent =
      '图片以 data URL 存在本地库里，单张上限：Logo ' +
      (state.branding && state.branding.limits ? state.branding.limits.logo_image_kb : 880) +
      ' KB、背景 ' +
      (state.branding && state.branding.limits ? state.branding.limits.bg_image_kb : 1600) +
      ' KB。备份数据库时会一并带走。';
  }

  function isDirty() {
    if (!state.draft || !state.branding) return false;
    var a = pickWritable(state.draft);
    var b = pickWritable(state.branding);
    return WRITABLE.some(function (key) {
      return String(a[key]) !== String(b[key]);
    });
  }

  /** 表单改动：更新草稿 → 立刻应用到页面（实时预览），但**不落库**。 */
  function patchDraft(changes, refill) {
    if (!state.draft) return;
    Object.keys(changes).forEach(function (key) {
      state.draft[key] = changes[key];
    });
    applyBranding(state.draft);
    if (refill) fillForm(state.draft);
    else {
      syncPresetHighlight();
      syncSegs();
    }
  }

  function openDrawer() {
    if (!state.branding) return;
    state.draft = clone(pickWritable(state.branding));
    fillForm(state.draft);
    $('drawer').hidden = false;
    $('drawer-mask').hidden = false;
    $('drawer-hint').textContent = '改动即时预览，点「保存」才写进配置';
  }

  function closeDrawer() {
    var hadChanges = isDirty();
    $('drawer').hidden = true;
    $('drawer-mask').hidden = true;
    state.draft = null;
    if (hadChanges) {
      // 没保存就走人 → 页面还原成已保存的样子，别留下一个「看起来改了其实没存」的状态
      applyBranding(state.branding);
      toast('未保存的改动已还原');
    }
  }

  var IMAGE_TYPES = /^image\/(png|jpe?g|webp|gif|bmp)$/;

  function readImageFile(file, limitBytes, onOk, onFail) {
    if (!IMAGE_TYPES.test(file.type)) {
      onFail('只支持 PNG / JPG / WebP / GIF / BMP 图片');
      return;
    }
    if (file.size > limitBytes) {
      onFail('这张图 ' + formatKb(file.size) + '，超过上限 ' + formatKb(limitBytes) + '，请先压缩再传');
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      onOk(String(reader.result));
    };
    reader.onerror = function () {
      onFail('读取文件失败，换个文件试试');
    };
    reader.readAsDataURL(file);
  }

  function saveBranding() {
    if (!state.draft) return;
    var payload = pickWritable(state.draft);
    api('/api/branding', { method: 'PUT', body: JSON.stringify(payload) })
      .then(function (data) {
        state.branding = data;
        state.draft = clone(pickWritable(data));
        applyBranding(data);
        fillForm(state.draft);
        toast('外观已保存');
        return loadEvents();
      })
      .catch(function (err) {
        toast('保存失败：' + err.message, true);
      });
  }

  function resetBranding() {
    api('/api/branding/reset', { method: 'POST' })
      .then(function (data) {
        state.branding = data;
        state.draft = clone(pickWritable(data));
        applyBranding(data);
        fillForm(state.draft);
        toast('已恢复出厂外观');
        return loadEvents();
      })
      .catch(function (err) {
        toast('恢复失败：' + err.message, true);
      });
  }

  function loadBranding() {
    return api('/api/branding').then(function (data) {
      state.branding = data;
      renderPresets();
    });
  }

  /* ------------------------------------------------------------ 渲染：概览 */
  function renderOverview() {
    var data = state.overview;
    var hero = $('hero');
    var body = $('overview-body');
    if (!data || !data.app_count) {
      hero.hidden = false;
      body.hidden = true;
      return;
    }
    hero.hidden = false;
    body.hidden = false;

    var metrics = [
      { label: '已登记应用', value: data.app_count, unit: '个', note: '含注册表与开始菜单两个来源' },
      {
        label: '已知占用合计',
        value: null,
        text: formatBytes(data.known_size_bytes),
        note: '安装程序自己登记的大小'
      },
      {
        label: '占用未知',
        value: data.unknown_size_count,
        unit: '个',
        note: '这些程序没登记大小，需要独立计算'
      },
      {
        label: '已忽略',
        value: data.ignored_count,
        unit: '个',
        note: '从清单里拿掉但仍在库中'
      }
    ];

    $('metrics').innerHTML = metrics
      .map(function (m) {
        var inner = m.text
          ? '<span class="metric-value">' + escapeHtml(m.text) + '</span>'
          : '<span class="metric-value">' + m.value + '</span>' +
            (m.unit ? '<span class="metric-unit">' + m.unit + '</span>' : '');
        return (
          '<div class="metric"><div class="metric-label">' + escapeHtml(m.label) + '</div>' +
          inner +
          '<div class="metric-note">' + escapeHtml(m.note) + '</div></div>'
        );
      })
      .join('');

    $('list-largest').innerHTML = (data.largest || [])
      .map(function (item) {
        return (
          '<li><span class="mini-name">' + escapeHtml(item.name) + '</span>' +
          '<span class="mini-meta">' + escapeHtml(item.publisher || '未知发布者') + '</span>' +
          '<span class="mini-num">' + escapeHtml(formatBytes(item.size_bytes)) + '</span></li>'
        );
      })
      .join('') || '<li class="muted">暂无数据</li>';

    $('list-recent').innerHTML = (data.recent_installed || [])
      .map(function (item) {
        return (
          '<li><span class="mini-name">' + escapeHtml(item.name) + '</span>' +
          '<span class="mini-meta mono">' + escapeHtml(item.install_date || '') + '</span>' +
          '<span class="mini-num">' + escapeHtml(item.version || '') + '</span></li>'
        );
      })
      .join('') || '<li class="muted">暂无数据</li>';

    $('list-events').innerHTML = (data.events || [])
      .slice(0, 8)
      .map(function (ev) {
        return (
          '<li><time>' + escapeHtml(ev.created_at) + '</time>' +
          '<span class="act">' + escapeHtml(ev.action) + '</span>' +
          '<span class="detail">' + escapeHtml(ev.target_name || '') + '</span></li>'
        );
      })
      .join('') || '<li class="muted">暂无记录</li>';
  }

  /* ------------------------------------------------------------ 渲染：应用清单 */
  function visibleApps() {
    var keyword = $('search').value.trim().toLowerCase();
    var source = $('filter-source').value;
    var showIgnored = $('show-ignored').checked;
    var sort = $('sort-by').value;

    var rows = state.apps.filter(function (item) {
      if (!showIgnored && item.ignored) return false;
      if (source && item.source !== source) return false;
      if (!keyword) return true;
      var haystack = [item.name, item.alias, item.publisher, item.path_or_url, item.version]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return haystack.indexOf(keyword) >= 0;
    });

    var comparators = {
      name: function (a, b) {
        return String(a.name).localeCompare(String(b.name), 'zh-Hans-CN');
      },
      size: function (a, b) {
        return (b.size_bytes || 0) - (a.size_bytes || 0);
      },
      installed: function (a, b) {
        return String(b.install_date || '').localeCompare(String(a.install_date || ''));
      },
      recent: function (a, b) {
        return String(b.last_used_at || '').localeCompare(String(a.last_used_at || ''));
      },
      used: function (a, b) {
        return (b.use_count || 0) - (a.use_count || 0);
      }
    };
    return rows.sort(comparators[sort] || comparators.name);
  }

  function actionButtons(item) {
    if (item.ignored) {
      return '<button type="button" class="btn btn-sm btn-ghost" data-act="restore">恢复</button>';
    }
    return (
      '<button type="button" class="btn btn-sm" data-act="launch">启动</button>' +
      '<button type="button" class="btn btn-sm btn-ghost" data-act="ignore">忽略</button>'
    );
  }

  function renderCards(rows) {
    $('apps-grid').innerHTML = rows
      .map(function (item, index) {
        var sizeText = formatBytes(item.size_bytes);
        // 只给前 24 张加错开的入场延迟：再往后延迟就长到影响观感了
        var delay = Math.min(index, 24) * 16;
        return (
          '<article class="app-card' + (item.ignored ? ' is-ignored' : '') + '" data-id="' + item.id + '"' +
          ' style="animation-delay:' + delay + 'ms">' +
          '<div class="app-card-top">' +
          '<span class="app-ico" style="' + iconStyle(item.name) + '">' +
          escapeHtml(initials(item.name)) +
          '</span>' +
          '<span class="app-name" title="' + escapeHtml(item.path_or_url || item.name) + '">' +
          escapeHtml(item.name) +
          '</span>' +
          '</div>' +
          '<div class="app-pub">' + escapeHtml(item.publisher || '未知发布者') + '</div>' +
          '<div class="app-foot">' +
          '<span>' + (sizeText === '—' ? '占用未知' : escapeHtml(sizeText)) + '</span>' +
          (item.version
            ? '<span>·</span><span class="app-ver" title="' + escapeHtml(item.version) + '">' +
              escapeHtml(item.version) + '</span>'
            : '') +
          '<span class="spacer"></span>' +
          '<span class="app-acts">' + actionButtons(item) + '</span>' +
          '</div>' +
          '</article>'
        );
      })
      .join('');
  }

  function renderTable(rows) {
    $('apps-tbody').innerHTML = rows
      .map(function (item) {
        var sizeText = formatBytes(item.size_bytes);
        var sizeCell = sizeText === '—'
          ? '<span class="muted">未知</span>'
          : '<span class="num">' + escapeHtml(sizeText) + '</span>';
        var sourceTag = item.source === 'registry'
          ? '<span class="tag tag-registry">已安装</span>'
          : '<span class="tag">开始菜单</span>';
        return (
          '<tr class="' + (item.ignored ? 'is-ignored' : '') + '" data-id="' + item.id + '">' +
          '<td class="col-name"><div class="cell-name">' +
          '<span class="avatar" style="' + iconStyle(item.name) + '">' +
          escapeHtml(initials(item.name)) +
          '</span>' +
          '<span title="' + escapeHtml(item.path_or_url || '') + '">' + escapeHtml(item.name) + '</span>' +
          '</div></td>' +
          '<td class="mono muted">' + escapeHtml(item.version || '—') + '</td>' +
          '<td class="muted">' + escapeHtml(item.publisher || '—') + '</td>' +
          '<td class="col-num">' + sizeCell + '</td>' +
          '<td class="mono muted">' + escapeHtml(item.install_date || '—') + '</td>' +
          '<td>' + sourceTag + '</td>' +
          '<td class="col-act"><div class="row-actions">' + actionButtons(item) + '</div></td></tr>'
        );
      })
      .join('');
  }

  function applyView() {
    var isGrid = state.view === 'grid';
    $('apps-grid').hidden = !isGrid;
    $('apps-table-wrap').hidden = isGrid;
    document.querySelectorAll('#view-switch button').forEach(function (el) {
      el.classList.toggle('is-active', el.dataset.view === state.view);
    });
  }

  function renderApps() {
    var rows = visibleApps();
    var empty = $('apps-empty');

    $('apps-count').textContent =
      '显示 ' + rows.length + ' / 共 ' + state.apps.length + ' 条' +
      (state.overview && state.overview.ignored_count
        ? '（已忽略 ' + state.overview.ignored_count + ' 条）'
        : '');

    if (!state.apps.length) {
      $('apps-grid').innerHTML = '';
      $('apps-tbody').innerHTML = '';
      $('apps-grid').hidden = true;
      $('apps-table-wrap').hidden = true;
      empty.hidden = false;
      empty.textContent = '还没有数据，点右上角「扫描本机」开始';
      return;
    }
    if (!rows.length) {
      $('apps-grid').innerHTML = '';
      $('apps-tbody').innerHTML = '';
      $('apps-grid').hidden = true;
      $('apps-table-wrap').hidden = true;
      empty.hidden = false;
      empty.textContent = '没有匹配的记录';
      return;
    }
    empty.hidden = true;
    renderCards(rows);
    renderTable(rows);
    applyView();
  }

  /* ============================================================ 模块面板 */

  var KIND_LABEL = {
    native: '同进程挂载',
    static: '托管静态产物',
    subprocess_proxy: '子进程 + 反代',
    external: '仅反代'
  };

  var MODULE_STATE = {
    running: '运行中',
    starting: '启动中…',
    stopping: '停止中…',
    stopped: '已停止',
    failed: '启动失败'
  };

  function moduleById(id) {
    return state.modules.filter(function (item) {
      return item.id === id;
    })[0];
  }

  function stateDot(item) {
    var cls = MODULE_STATE[item.state] ? item.state : 'stopped';
    return '<span class="state-dot is-' + cls + '" title="' + escapeHtml(moduleStateText(item)) + '"></span>';
  }

  function moduleStateText(item) {
    var text = MODULE_STATE[item.state] || item.state;
    if (item.state === 'running' && item.attached) return text + '（接管自外部进程）';
    if (!item.enabled) return '已禁用';
    return text;
  }

  function moduleMessages(item) {
    var out = [];
    if (!item.enabled) {
      out.push(['is-warn', '这个模块在 module.py 里是 enabled=False，中台不为它提供代理。']);
    }
    if (item.spec_stale) {
      out.push(['is-warn', 'module.py 改过了，但进程还是按旧声明起的 —— 重启这个模块才会生效。']);
    }
    if (item.error) {
      out.push(['is-error', item.error]);
    }
    if (item.state === 'running' && item.attached) {
      out.push([
        'is-warn',
        '这个进程不是中台拉起的（内部端口本来就有人监听，健康检查通过后接管）。' +
          '中台退出时不会去收它 —— 要停请自己按 PID 结束。'
      ]);
    }
    if (item.state === 'running' && item.allow_realtime === false) {
      out.push([
        'is-warn',
        '实时通道（socket.io / WebSocket）未放行。模块里挂在实时通道上的能力（例如' +
          '「在线执行脚本」）会明确报错而不是静默失败 —— 那类能力等同于任意命令执行入口。'
      ]);
    }
    return out;
  }

  function renderModules() {
    var host = $('module-list');
    var count = $('modules-count');
    if (!state.modules.length) {
      host.innerHTML = '';
      count.textContent = '';
      $('modules-empty').hidden = false;
      $('module-errors').hidden = true;
      return;
    }
    $('modules-empty').hidden = true;

    var running = state.modules.filter(function (m) {
      return m.state === 'running';
    }).length;
    count.textContent = '共 ' + state.modules.length + ' 个，运行中 ' + running + ' 个';

    // 声明加载失败要单独顶到上面 —— 这类错误是在 modules/ 目录里写错了东西，
    // 混在卡片里很容易被忽略，而它会让一整个模块直接不出现。
    var errorKeys = Object.keys(state.moduleErrors || {});
    var errorBox = $('module-errors');
    if (errorKeys.length) {
      errorBox.hidden = false;
      errorBox.innerHTML =
        '<b>有 ' +
        errorKeys.length +
        ' 个模块的声明没通过校验，已被跳过：</b>\n' +
        errorKeys
          .map(function (key) {
            return escapeHtml(key) + ' —— ' + escapeHtml(state.moduleErrors[key]);
          })
          .join('\n');
    } else {
      errorBox.hidden = true;
    }

    host.innerHTML = state.modules.map(renderModuleCard).join('');
  }

  function renderModuleCard(item) {
    var msgs = moduleMessages(item)
      .map(function (pair) {
        return '<div class="module-msg ' + pair[0] + '">' + escapeHtml(pair[1]) + '</div>';
      })
      .join('');

    var canOpen = item.proxy_ready;
    var canStart = item.state === 'stopped' || item.state === 'failed';
    var canStop = item.state === 'running' && !item.attached;
    var canRestart = item.state === 'running' || item.state === 'failed';

    function btn(act, label, enabled, primary) {
      return (
        '<button type="button" class="btn btn-sm ' +
        (primary ? 'btn-primary' : 'btn-ghost') +
        '" data-act="' + act + '"' + (enabled ? '' : ' disabled') + '>' +
        escapeHtml(label) +
        '</button>'
      );
    }

    var meta = [
      '<span class="kv">接入方式 <b>' + escapeHtml(KIND_LABEL[item.kind] || item.kind) + '</b></span>',
      '<span class="kv">对外路径 <b>' + escapeHtml(item.mount) + '/</b></span>'
    ];
    if (item.internal_port) {
      meta.push('<span class="kv">内部端口 <b>127.0.0.1:' + item.internal_port + '</b></span>');
    }
    if (item.pid) {
      meta.push('<span class="kv">PID <b>' + item.pid + '</b></span>');
    }
    if (item.version) {
      meta.push('<span class="kv">版本 <b>' + escapeHtml(item.version) + '</b></span>');
    }

    return (
      '<article class="module-card' + (item.state === 'failed' ? ' is-failed' : '') +
      '" data-module="' + escapeHtml(item.id) + '">' +
      '<div class="module-head">' +
      stateDot(item) +
      '<span class="module-ico">' + escapeHtml(initials(item.name)) + '</span>' +
      '<div class="module-title">' +
      '<h3>' + escapeHtml(item.name) +
      '<span class="tag">' + escapeHtml(moduleStateText(item)) + '</span>' +
      '</h3>' +
      '<p>' + escapeHtml(item.description || '（未写描述）') + '</p>' +
      '</div>' +
      '</div>' +
      '<div class="module-meta">' + meta.join('') + '</div>' +
      msgs +
      '<div class="module-acts">' +
      btn('open', '打开', canOpen, true) +
      btn('start', '启动', canStart) +
      btn('stop', '停止', canStop) +
      btn('restart', '重启', canRestart) +
      btn('log', '日志', true) +
      '<span class="spacer"></span>' +
      (item.source_url
        ? '<a class="btn btn-sm btn-ghost" href="' + escapeHtml(item.source_url) +
          '" target="_blank" rel="noreferrer noopener" title="上游出处（许可要求保留）">出处</a>'
        : '') +
      '</div>' +
      '</article>'
    );
  }

  function loadModules() {
    return api('/api/modules').then(function (data) {
      state.modules = data.items || [];
      state.moduleErrors = data.errors || {};
      renderModules();
    });
  }

  /** 打开模块：用 iframe 把它嵌在中台里，顶栏留着不遮。
   *  src 用中台给的 open_url（带模块级票据）—— 不是中台令牌。
   *  票据只在这一次进门用，随后中台会种一个 HttpOnly 的模块 Cookie。 */
  function openModule(id) {
    var item = moduleById(id);
    if (!item) return;
    if (!item.proxy_ready) {
      toast('模块还没就绪：' + (item.error || moduleStateText(item)), true);
      return;
    }
    state.openModuleId = id;
    $('runner-title').textContent = item.name;
    $('runner-sub').textContent = item.mount + '/';
    $('runner-dot').className = 'state-dot is-' + (MODULE_STATE[item.state] ? item.state : 'stopped');
    $('runner-frame').src = item.open_url;
    $('runner').hidden = false;
  }

  function closeRunner() {
    if (state.openModuleId === null) return;
    state.openModuleId = null;
    // 先把 src 清掉再隐藏：否则 iframe 里的定时器/轮询会继续跑
    $('runner-frame').src = 'about:blank';
    $('runner').hidden = true;
  }

  function moduleAction(id, act) {
    var item = moduleById(id);
    if (!item) return;
    var verb = act === 'stop' ? '停止' : act === 'restart' ? '重启' : '启动';
    toast('正在' + verb + ' ' + item.name + ' …');

    // 立刻把卡片切到过渡态：子进程探活最长要等 30 秒，
    // 期间按钮纹丝不动会让人以为没点上。
    item.state = act === 'start' ? 'starting' : 'stopping';
    item.error = '';
    renderModules();
    syncModulePolling();

    api('/api/modules/' + encodeURIComponent(id) + '/' + act, { method: 'POST' })
      .then(function (res) {
        if (res.message) toast(res.message);
        return loadModules();
      })
      .catch(function (err) {
        toast('操作失败：' + err.message, true);
        return loadModules();
      });
  }

  function reloadModules() {
    api('/api/modules/reload', { method: 'POST' })
      .then(function (data) {
        var failed = Object.keys(data.errors || {}).length;
        toast('已重新扫描 modules/ 目录：加载 ' + (data.loaded || []).length + ' 个' +
          (failed ? '，' + failed + ' 个声明有问题' : ''));
        return loadModules();
      })
      .catch(function (err) {
        toast('重载失败：' + err.message, true);
      });
  }

  function openModuleLog(id) {
    var item = moduleById(id);
    if (!item) return;
    state.logModuleId = id;
    $('log-title').textContent = item.name + ' · 运行日志';
    $('log-note').textContent =
      '只看得到这个模块自己的 stdout/stderr 与中台的接管记录，' +
      '与中台自身的请求日志是分开的。内部端口 127.0.0.1:' + (item.internal_port || '—') + '。';
    $('modal-mask').hidden = false;
    $('module-log-modal').hidden = false;
    loadModuleLog();
  }

  function loadModuleLog() {
    var id = state.logModuleId;
    if (!id) return Promise.resolve();
    $('log-view').textContent = '…';
    return api('/api/modules/' + encodeURIComponent(id) + '/log?lines=400')
      .then(function (data) {
        $('log-view').textContent = (data.lines || []).join('\n') || '（暂无输出）';
        $('log-path').textContent = data.log_file || '';
        $('log-view').scrollTop = $('log-view').scrollHeight;
      })
      .catch(function (err) {
        $('log-view').textContent = '读取日志失败：' + err.message;
      });
  }

  function closeModuleLog() {
    state.logModuleId = null;
    $('modal-mask').hidden = true;
    $('module-log-modal').hidden = true;
  }

  /** 模块面板打开时轮询状态：子进程从「启动中」到「运行中」是异步的，
   *  不轮询的话卡片会一直停在你点下去那一刻的样子。 */
  function syncModulePolling() {
    var want = state.tab === 'modules' || state.openModuleId !== null;
    if (want && !state.moduleTimer) {
      state.moduleTimer = setInterval(function () {
        if (state.tab !== 'modules' && state.openModuleId === null) return;
        loadModules()
          .then(function () {
            if (state.openModuleId !== null) {
              var item = moduleById(state.openModuleId);
              if (item) {
                $('runner-dot').className =
                  'state-dot is-' + (MODULE_STATE[item.state] ? item.state : 'stopped');
                $('runner-sub').textContent = item.mount + '/ · ' + moduleStateText(item);
              }
            }
          })
          .catch(function () {});
      }, 4000);
    } else if (!want && state.moduleTimer) {
      clearInterval(state.moduleTimer);
      state.moduleTimer = null;
    }
  }

  /* ------------------------------------------------------------ 渲染：审计 */
  function renderAudit() {
    var tbody = $('events-tbody');
    var empty = $('events-empty');
    if (!state.events.length) {
      tbody.innerHTML = '';
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    tbody.innerHTML = state.events
      .map(function (ev) {
        var detail = '';
        if (ev.detail) {
          try {
            var parsed = JSON.parse(ev.detail);
            detail = parsed && parsed.message ? parsed.message : '';
            if (!detail && parsed && parsed.scan) {
              detail = '注册表 ' + parsed.scan.registry_total + ' 条，' +
                '开始菜单补充 ' + parsed.scan.startmenu_extra + ' 条';
            }
            if (!detail && parsed && parsed.fields) {
              detail = '改了：' + parsed.fields.join('、');
            }
          } catch (err) {
            detail = '';
          }
        }
        return (
          '<tr><td class="col-time">' + escapeHtml(ev.created_at) + '</td>' +
          '<td><span class="tag tag-registry">' + escapeHtml(ev.action) + '</span></td>' +
          '<td>' + escapeHtml(ev.target_name || '—') + '</td>' +
          '<td class="muted">' + escapeHtml(detail) + '</td></tr>'
        );
      })
      .join('');
  }

  /* ------------------------------------------------------------ 数据加载 */
  function loadOverview() {
    return api('/api/overview').then(function (data) {
      state.overview = data;
      renderOverview();
      refreshPill();
    });
  }

  function loadApps() {
    return api(API_APPS + '?include_ignored=true&limit=20000').then(function (data) {
      state.apps = data.items || [];
      renderApps();
    });
  }

  function loadEvents() {
    return api('/api/events?limit=200').then(function (data) {
      state.events = data.items || [];
      renderAudit();
    });
  }

  function refreshAll() {
    return Promise.all([loadOverview(), loadApps(), loadEvents()]);
  }

  function refreshPill() {
    var pill = $('local-pill');
    if (!state.overview) return;
    var parts = [];
    if (state.overview.machine_id) parts.push(state.overview.machine_id);
    var last = state.overview.last_scan;
    parts.push(last ? '上次扫描 ' + (last.finished_at || last.started_at) : '尚未扫描');
    pill.textContent = parts.join(' · ');
  }

  /* ------------------------------------------------------------ 动作 */
  function doScan() {
    if (state.busy) return;
    setBusy(true, '扫描中…');
    toast('正在扫描本机已安装程序，请稍候…');
    api(API_APPS + '/scan', { method: 'POST' })
      .then(function (res) {
        var s = res.scan;
        toast(
          '扫描完成：注册表 ' + s.registry_total + ' 条' +
          '（新增 ' + res.registry.inserted + ' / 更新 ' + res.registry.updated + '），' +
          '开始菜单补充 ' + s.startmenu_extra + ' 条，耗时 ' + res.duration_ms + ' ms'
        );
        return refreshAll();
      })
      .catch(function (err) {
        toast(err.message || '扫描失败', true);
      })
      .then(function () {
        setBusy(false);
      });
  }

  function doLaunch(id) {
    var row = state.apps.filter(function (item) {
      return String(item.id) === String(id);
    })[0];
    toast('正在启动 ' + (row ? row.name : '') + ' …');
    api(API_APPS + '/' + id + '/launch', { method: 'POST' })
      .then(function () {
        toast('已发出启动指令：' + (row ? row.name : ''));
        return Promise.all([loadApps(), loadEvents()]);
      })
      .catch(function (err) {
        toast('启动失败：' + err.message, true);
      });
  }

  function setIgnored(id, ignored) {
    api(API_APPS + '/' + id, {
      method: 'PATCH',
      body: JSON.stringify({ ignored: ignored })
    })
      .then(function () {
        return Promise.all([loadApps(), loadOverview(), loadEvents()]);
      })
      .catch(function (err) {
        toast('操作失败：' + err.message, true);
      });
  }

  /* ------------------------------------------------------------ 标签页 */
  function moveGlider() {
    var active = document.querySelector('.tab.is-active');
    var glider = $('tab-glider');
    if (!active || !glider) return;
    glider.style.width = active.offsetWidth + 'px';
    glider.style.transform = 'translateX(' + (active.offsetLeft - 3) + 'px)';
  }

  function gotoPanel(name) {
    document.querySelectorAll('.tab').forEach(function (el) {
      el.classList.toggle('is-active', el.dataset.tab === name);
    });
    document.querySelectorAll('.panel').forEach(function (el) {
      el.classList.toggle('is-active', el.id === 'panel-' + name);
    });
    state.tab = name;
    // 切走时把模块运行区收掉：它是盖上来的整块区域，留着会把新面板挡住
    if (name !== 'modules') closeRunner();
    syncModulePolling();
    moveGlider();
  }

  /** 支持 ?tab=apps&view=grid&module=opsgen 深链接，方便把某个视图直接存成书签。 */
  function readUrlPrefs() {
    var params = new URLSearchParams(location.search);
    if (['overview', 'apps', 'modules', 'audit'].indexOf(params.get('tab')) >= 0) {
      state.tab = params.get('tab');
    }
    var view = params.get('view');
    // 只影响本次打开，不写 localStorage —— 免得临时看一眼表格就把默认视图改了
    if (view === 'grid' || view === 'table') state.view = view;

    // ?module=<id>：直接打开某个模块。**不能在这里就开** —— 打开要用模块的
    // open_url（里面带它这一轮的票据），那份数据要等列表拉回来才有。
    // 所以先记下意图，等 loadModules() 完成再兑现。
    var wantModule = params.get('module');
    if (wantModule) {
      state.tab = 'modules';
      state.pendingModule = wantModule;
    }
  }

  /* ------------------------------------------------------------ 登录门绑定 */
  // 单独拎出来，是因为它必须在 boot() 的一开始就绑好 —— 那时 startApp()
  // 可能还没跑过（没登录），但门已经推出来了，里面的按钮必须是活的。
  function bindLogin() {
    var form = $('login-form');
    if (form) {
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        var value = extractToken($('login-token').value);
        if (!value) {
          setLoginError('先粘贴令牌，或者把整条访问地址粘进来。');
          return;
        }
        var button = $('login-submit');
        button.disabled = true;
        button.textContent = '验证中…';
        submitSession(value)
          .then(function () {
            TOKEN = value;
            hideLoginGate();
            // 从这儿重来一遍，让所有面板都去拉一次数据
            startApp();
          })
          .catch(function (err) {
            setLoginError(err.message + '。令牌在 start.bat 窗口打印的访问地址里。');
          })
          .then(function () {
            button.disabled = false;
            button.textContent = '进入';
          });
      });
    }

    var logoutBtn = $('btn-logout');
    if (logoutBtn) logoutBtn.addEventListener('click', logout);
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind() {
    $('tabs').addEventListener('click', function (event) {
      var tab = event.target.closest('.tab');
      if (!tab) return;
      gotoPanel(tab.dataset.tab);
      if (tab.dataset.tab === 'audit') loadEvents().catch(function () {});
    });

    document.querySelectorAll('[data-goto]').forEach(function (el) {
      el.addEventListener('click', function () {
        gotoPanel(el.dataset.goto);
      });
    });

    ['btn-scan', 'btn-scan-hero'].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener('click', doScan);
    });

    $('search').addEventListener('input', renderApps);
    $('filter-source').addEventListener('change', renderApps);
    $('sort-by').addEventListener('change', renderApps);
    $('show-ignored').addEventListener('change', renderApps);

    // 卡片与表格共用同一套动作：都从最近的 [data-id] 容器上取 id
    ['apps-grid', 'apps-tbody'].forEach(function (host) {
      $(host).addEventListener('click', function (event) {
        var button = event.target.closest('button[data-act]');
        if (!button) return;
        var holder = button.closest('[data-id]');
        if (!holder) return;
        var id = holder.dataset.id;
        var act = button.dataset.act;
        if (act === 'launch') doLaunch(id);
        else if (act === 'ignore') setIgnored(id, true);
        else if (act === 'restore') setIgnored(id, false);
      });
    });

    $('view-switch').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-view]');
      if (!button) return;
      state.view = button.dataset.view;
      localStorage.setItem(VIEW_KEY, state.view);
      applyView();
    });

    /* ---- 外观抽屉 ---- */
    $('btn-appearance').addEventListener('click', openDrawer);
    $('drawer-close').addEventListener('click', closeDrawer);
    $('drawer-mask').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!$('module-log-modal').hidden) closeModuleLog();
      else if (!$('drawer').hidden) closeDrawer();
    });

    $('f-app-name').addEventListener('input', function () {
      patchDraft({ app_name: this.value });
    });
    $('f-tagline').addEventListener('input', function () {
      patchDraft({ tagline: this.value });
    });
    $('f-logo-text').addEventListener('input', function () {
      patchDraft({ logo_text: this.value });
    });

    ['accent', 'accent2', 'bg', 'surface', 'text'].forEach(function (key) {
      $('f-' + key).addEventListener('input', function () {
        var changes = {};
        changes[key] = this.value;
        patchDraft(changes);
      });
    });

    $('f-overlay').addEventListener('input', function () {
      $('overlay-value').textContent = this.value;
      patchDraft({ bg_overlay: Number(this.value) });
    });

    $('preset-row').addEventListener('click', function (event) {
      var button = event.target.closest('.preset');
      if (!button) return;
      var presets = (state.branding && state.branding.presets) || {};
      var chosen = presets[button.dataset.preset];
      if (!chosen) return;
      patchDraft(
        { preset: button.dataset.preset, accent: chosen.accent, accent2: chosen.accent2 },
        true
      );
    });

    $('mode-switch').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-mode]');
      if (!button) return;
      var mode = button.dataset.mode;
      var base = state.branding || {};
      var changes = { mode: mode };
      // 与后端同一套规则：当前值还等于「本模式默认值」就说明用户没动过，
      // 跟着换成另一套默认值；动过就保留。
      var from = mode === 'dark' ? base.light_base : base.dark_base;
      var to = mode === 'dark' ? base.dark_base : base.light_base;
      if (from && to) {
        ['bg', 'surface', 'text'].forEach(function (key) {
          if (String(state.draft[key]).toLowerCase() === String(from[key]).toLowerCase()) {
            changes[key] = to[key];
          }
        });
      }
      patchDraft(changes, true);
    });

    $('bgstyle-switch').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-bg]');
      if (!button) return;
      patchDraft({ bg_style: button.dataset.bg }, true);
    });

    $('f-logo-file').addEventListener('change', function () {
      var file = this.files && this.files[0];
      this.value = '';
      if (!file) return;
      readImageFile(
        file,
        620 * 1024,
        function (dataUrl) {
          patchDraft({ logo_image: dataUrl }, true);
        },
        function (message) {
          toast(message, true);
        }
      );
    });

    $('f-bg-file').addEventListener('change', function () {
      var file = this.files && this.files[0];
      this.value = '';
      if (!file) return;
      readImageFile(
        file,
        1200 * 1024,
        function (dataUrl) {
          patchDraft({ bg_image: dataUrl }, true);
        },
        function (message) {
          toast(message, true);
        }
      );
    });

    $('btn-logo-clear').addEventListener('click', function () {
      patchDraft({ logo_image: '' }, true);
    });
    $('btn-bg-clear').addEventListener('click', function () {
      patchDraft({ bg_image: '' }, true);
    });

    $('btn-brand-save').addEventListener('click', saveBranding);
    $('btn-brand-reset').addEventListener('click', resetBranding);

    /* ---- 模块面板 ---- */
    $('module-list').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-act]');
      if (!button) return;
      var card = button.closest('[data-module]');
      if (!card) return;
      var id = card.dataset.module;
      var act = button.dataset.act;
      if (act === 'open') openModule(id);
      else if (act === 'log') openModuleLog(id);
      else moduleAction(id, act);
    });

    $('btn-modules-reload').addEventListener('click', reloadModules);

    $('runner-back').addEventListener('click', closeRunner);
    $('runner-reload').addEventListener('click', function () {
      if (state.openModuleId === null) return;
      var frame = $('runner-frame');
      // 用 src 原样重设会命中浏览器缓存也可能不触发重载，走 location.reload 更确定
      try {
        frame.contentWindow.location.reload();
      } catch (err) {
        var item = moduleById(state.openModuleId);
        if (item) frame.src = item.open_url;
      }
    });
    $('runner-log').addEventListener('click', function () {
      if (state.openModuleId !== null) openModuleLog(state.openModuleId);
    });
    $('runner-external').addEventListener('click', function () {
      var item = moduleById(state.openModuleId);
      if (item) window.open(item.open_url, '_blank', 'noopener');
    });

    $('log-close').addEventListener('click', closeModuleLog);
    $('modal-mask').addEventListener('click', closeModuleLog);
    $('log-refresh').addEventListener('click', loadModuleLog);

    window.addEventListener('resize', moveGlider);
    // 字体加载完成后按钮宽度会变，滑块的宽度得跟着重算一次
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(moveGlider).catch(function () {});
    }
  }

  /* ------------------------------------------------------------ 启动 */
  // bind() 只该跑一次，但 startApp() 会在「登录成功后」再跑一遍 ——
  // 所以把一次性的事情用这个标志隔开。重复 bind 会让每个按钮响应两次。
  var bound = false;

  function startApp() {
    if (!bound) {
      bound = true;
      readUrlPrefs();
      bind();
      applyView();
      gotoPanel(state.tab);
    }
    // 服务端已经把品牌注入首页了，这里只是把配置取回来供外观面板使用
    loadBranding().catch(function (err) {
      toast('读取外观配置失败：' + err.message, true);
    });
    refreshAll().catch(function (err) {
      toast('初始化失败：' + err.message, true);
    });
    loadModules()
      .then(function () {
        // ?module=<id> 的兑现点：此刻模块列表与 open_url 都到手了
        if (state.pendingModule) {
          var wanted = state.pendingModule;
          state.pendingModule = null;
          openModule(wanted);
        }
        syncModulePolling();
      })
      .catch(function (err) {
        toast('读取模块列表失败：' + err.message, true);
      });
  }

  function boot() {
    if (!bound) {
      // 登录门上的表单与退出按钮要**现在就**绑好 —— 下面可能直接把门推出来，
      // 而那时 startApp() 还没跑过，按钮会是死的。
      bindLogin();
    }

    // 三条路，按代价从低到高试：
    //   1. 已经登录过  → 会话 Cookie 直接放行，什么都不用做
    //   2. 入口地址带令牌 → 静默换一条会话，用户根本看不到登录门
    //   3. 都没有      → 把门推出来
    if (TOKEN) {
      submitSession(TOKEN)
        .then(function () {
          hideLoginGate();
          startApp();
        })
        .catch(function () {
          // 手里的令牌是旧的（换过数据目录、删过 data/.token 之类）。
          // 别急着报错 —— 先看看是不是已经有一条有效会话，有就照样进去。
          checkSession().then(function (data) {
            if (data.authenticated) {
              TOKEN = '';
              hideLoginGate();
              startApp();
            } else {
              showLoginGate('地址里带的令牌已经失效，请粘贴当前那条。');
            }
          });
        });
      return;
    }

    checkSession()
      .then(function (data) {
        if (data.authenticated) {
          hideLoginGate();
          startApp();
        } else {
          showLoginGate();
        }
      })
      .catch(function () {
        showLoginGate('读取会话状态失败，请手动输入令牌。');
      });
  }

  boot();
})();
