/* 日志可视化分析 —— 模块自带脚本。
 *
 * 无构建、无框架、无外部依赖：改完直接刷 iframe 就生效。
 *
 * ### 流程
 *
 *   登记日志目录 → 列目录 → 点文件 → **先认出类型**（可以改）→ 出报告
 *                                                  → 点指标 → 抽屉里看原始行
 *
 * 「先认出类型」刻意做成独立一步：第一版只支持三类，认错是迟早的事。
 * 让用户能看一眼分数、换一个类型再解析，比让他对着一份错误类型的报告
 * 琢磨"为什么数字这么怪"强得多。
 *
 * ### 报告有三种形状
 *
 * nginx / java / mysql 三份报告的结构完全不同，所以渲染也是三段独立的代码。
 * 共同的只有：KPI 条、时间直方图、以及「每行都能点」这件事。
 *
 * 所有请求都带同源 Cookie（中台在首次进入模块时种下的模块票据），
 * 所以这里不需要、也拿不到中台令牌。
 */
(function () {
  'use strict';

  var MOUNT = (window.__LV__ && window.__LV__.mount) || '/logviz';
  var API = MOUNT + '/api';

  //: 类型的显示名兜底。后端 /api/roots 会带一份权威的 labels 回来，
  //: 但第一次渲染时可能还没拿到，所以本地也放一份。
  var KIND_NAMES = {
    nginx: 'nginx access 日志',
    java: 'Java 应用日志',
    mysql: 'MySQL 慢查询日志'
  };

  var state = {
    roots: [],
    rootId: '',
    rootName: '',
    path: '',
    search: '',
    files: { dirs: [], files: [] },
    // 当前选中的文件
    target: null,      // { path, name, size }
    detect: null,      // /api/detect 的结果
    report: null,      // /api/analyze 的结果
    kindChoice: '',    // 用户手动选的类型
    busy: false
  };

  /* ------------------------------------------------------------ 工具 */
  function $(id) {
    return document.getElementById(id);
  }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function num(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString('zh-CN');
  }

  function bytes(n) {
    if (n == null) return '—';
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB', 'PB'];
    var value = n / 1024;
    var i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i += 1;
    }
    return (value >= 100 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[i];
  }

  function ratio(n) {
    if (n == null) return '—';
    return (n * 100).toFixed(1) + '%';
  }

  function request(path, options) {
    var opts = options || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(API + path, init).then(function (res) {
      return res.json().catch(function () {
        return {};
      }).then(function (data) {
        if (!res.ok) {
          var detail = data && (data.detail || data.message);
          throw new Error(typeof detail === 'string' ? detail : '请求失败（HTTP ' + res.status + '）');
        }
        return data;
      });
    });
  }

  function kindName(kind) {
    return KIND_NAMES[kind] || kind || '未知类型';
  }

  /* ------------------------------------------------------------ 目录名单 */
  function loadRoots(keepSelection) {
    return request('/roots').then(function (data) {
      state.roots = data.items || [];
      if (data.labels) {
        Object.keys(data.labels).forEach(function (key) {
          if (data.labels[key]) KIND_NAMES[key] = data.labels[key];
        });
      }
      var stillThere = state.roots.some(function (r) {
        return r.id === state.rootId;
      });
      if (!stillThere) {
        state.rootId = state.roots.length ? state.roots[0].id : '';
      }
      drawRoots();
      if (state.rootId && (!keepSelection || !stillThere)) {
        selectRoot(state.rootId);
      } else if (!state.rootId) {
        state.path = '';
        state.files = { dirs: [], files: [] };
        drawFiles();
        drawCrumbs();
      }
    });
  }

  function drawRoots() {
    var list = $('lv-roots');
    list.innerHTML = '';
    if (!state.roots.length) {
      var li0 = document.createElement('li');
      li0.className = 'lv-root';
      li0.innerHTML = '<div class="lv-root-body"><span class="lv-root-path">还没有登记日志目录</span></div>';
      list.appendChild(li0);
      return;
    }
    state.roots.forEach(function (root) {
      var li = document.createElement('li');
      li.className = 'lv-root' + (root.id === state.rootId ? ' is-active' : '') +
        (root.exists ? '' : ' is-missing');
      li.innerHTML =
        '<div class="lv-root-body">' +
        '<span class="lv-root-name">' + esc(root.name) + '</span>' +
        '<span class="lv-root-path" title="' + esc(root.path) + '">' + esc(root.path) + '</span>' +
        (root.exists ? '' : '<span class="lv-root-flag">目录不可访问（盘没挂上或被改名了）</span>') +
        '</div>' +
        '<button type="button" class="lv-root-x" title="移出名单（不动磁盘）">×</button>';
      li.addEventListener('click', function (event) {
        if (event.target.classList.contains('lv-root-x')) return;
        selectRoot(root.id);
      });
      li.querySelector('.lv-root-x').addEventListener('click', function (event) {
        event.stopPropagation();
        removeRoot(root);
      });
      list.appendChild(li);
    });
  }

  function selectRoot(rootId) {
    var root = state.roots.filter(function (r) {
      return r.id === rootId;
    })[0];
    if (!root) return;
    state.rootId = rootId;
    state.rootName = root.name;
    state.path = '';
    state.search = '';
    state.target = null;
    state.detect = null;
    state.report = null;
    $('lv-search').value = '';
    drawRoots();
    loadDir('');
    clearReport();
  }

  function removeRoot(root) {
    if (!window.confirm('把「' + root.name + '」移出名单？\n\n只会取消登记，磁盘上的目录不会被改动。')) {
      return;
    }
    request('/roots/' + encodeURIComponent(root.id), { method: 'DELETE' }).then(function () {
      if (state.rootId === root.id) {
        state.rootId = '';
        state.path = '';
      }
      return loadRoots(false);
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  /* ------------------------------------------------------------ 列文件 */
  function loadDir(rel) {
    if (!state.rootId) return;
    state.path = rel || '';
    var box = $('lv-filelist');
    box.innerHTML = '<p class="lv-note">正在读取…</p>';
    drawCrumbs();

    var params = new URLSearchParams();
    params.set('root', state.rootId);
    params.set('path', state.path);
    if (state.search) params.set('search', state.search);

    request('/list?' + params.toString()).then(function (data) {
      state.files = { dirs: data.dirs || [], files: data.files || [] };
      drawFiles();
    }).catch(function (err) {
      box.innerHTML = '<p class="lv-error">读不了这一层：' + esc(err.message) + '</p>';
    });
  }

  function drawCrumbs() {
    var box = $('lv-crumbs');
    box.innerHTML = '';
    if (!state.rootId) return;

    var mk = function (label, target) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'lv-crumb';
      b.textContent = label;
      b.title = label;
      b.addEventListener('click', function () {
        loadDir(target);
      });
      return b;
    };

    box.appendChild(mk(state.rootName || '根', ''));
    var acc = [];
    (state.path ? state.path.split('/') : []).forEach(function (piece) {
      acc.push(piece);
      var sep = document.createElement('span');
      sep.className = 'lv-crumb-sep';
      sep.textContent = '/';
      box.appendChild(sep);
      box.appendChild(mk(piece, acc.join('/')));
    });
  }

  function drawFiles() {
    var box = $('lv-filelist');
    box.innerHTML = '';
    var stat = $('lv-files-stat');

    var dirs = state.files.dirs || [];
    var files = state.files.files || [];
    if (!dirs.length && !files.length) {
      box.innerHTML = '<p class="lv-empty">' +
        (state.search ? '本层没有匹配「' + esc(state.search) + '」的日志' : '（这一层是空的）') +
        '</p>';
      stat.textContent = '';
      return;
    }

    dirs.forEach(function (item) {
      var row = document.createElement('div');
      row.className = 'lv-file is-dir';
      row.innerHTML =
        '<div class="lv-file-main">' +
        '<span class="lv-file-name">' + esc(item.name) + '/</span>' +
        '<span class="lv-file-meta">目录</span>' +
        '</div>' +
        '<span class="lv-file-hint">进入</span>';
      row.addEventListener('click', function () {
        loadDir(item.path);
      });
      box.appendChild(row);
    });

    files.forEach(function (item) {
      var row = document.createElement('div');
      row.className = 'lv-file' + (state.target && state.target.path === item.path ? ' is-active' : '');
      var hintClass = 'lv-file-hint';
      if (item.score >= 100) hintClass += ' is-log';
      else if (item.score >= 40) hintClass += ' is-maybe';
      else if (item.score < 0) hintClass += ' is-warn';
      var hintText = item.hint || '不确定';
      if (item.outside) hintText = '外部链接';

      row.innerHTML =
        '<div class="lv-file-main">' +
        '<span class="lv-file-name" title="' + esc(item.name) + '">' + esc(item.name) + '</span>' +
        '<span class="lv-file-meta">' + esc(bytes(item.size)) + ' · ' + esc(fmtTime(item.mtime)) + '</span>' +
        '</div>' +
        '<span class="' + hintClass + '">' + esc(hintText) + '</span>';
      if (item.outside) {
        row.title = '这个条目指向登记目录之外，不会被读取';
      } else {
        row.addEventListener('click', function () {
          pickFile(item);
        });
      }
      box.appendChild(row);
    });

    stat.textContent = dirs.length + ' 目录 / ' + files.length + ' 文件';
  }

  function fmtTime(seconds) {
    if (!seconds) return '';
    var d = new Date(seconds * 1000);
    var pad = function (n) {
      return (n < 10 ? '0' : '') + n;
    };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /* ------------------------------------------------------------ 选文件 */
  function pickFile(item) {
    if (state.busy) return;
    state.target = { path: item.path, name: item.name, size: item.size };
    state.detect = null;
    state.report = null;
    state.kindChoice = '';
    drawFiles();
    renderBusy('正在认出这份文件是什么日志…');

    request('/detect', { method: 'POST', body: { root: state.rootId, path: item.path } })
      .then(function (data) {
        state.detect = data;
        renderDetect();
      })
      .catch(function (err) {
        renderError('认不出这份文件：' + err.message);
      });
  }

  function renderBusy(text) {
    $('lv-report').innerHTML = '<p class="lv-note">' + esc(text) + '</p>';
    $('lv-stat').textContent = '';
  }

  function renderError(text) {
    $('lv-report').innerHTML = '<p class="lv-error">' + esc(text) + '</p>';
    $('lv-stat').textContent = '';
  }

  function clearReport() {
    $('lv-report').innerHTML =
      '<p class="lv-empty" id="lv-report-empty">中间选一个文件，先认出它是什么日志，再出报告。</p>';
    $('lv-stat').textContent = '';
  }

  /* ------------------------------------------------------------ 检测结果 */
  function renderDetect() {
    var d = state.detect;
    if (!d) return;
    var rank = Object.keys(d.scores || {}).sort(function (a, b) {
      return (d.scores[b] || 0) - (d.scores[a] || 0);
    });
    var best = d.best || rank[0] || '';
    var chosen = state.kindChoice || d.best || '';

    var scan = d.scan || {};
    var choose = document.createElement('div');
    choose.className = 'lv-choose';
    var title = d.detected
      ? '认出来了：' + esc(kindName(d.best)) + '（把握 ' + ratio(d.confidence) + '）'
      : '认不出这份日志的类型';
    choose.innerHTML =
      '<h3>' + title + '</h3>' +
      '<p>' +
      (d.detected
        ? '如果不对，换一个再解析 —— 认错类型出来的报告比没有报告更危险。'
        : '三类都不太像。请手动指定它是哪一类 —— 认错类型出来的报告比没有报告更危险。') +
      '</p>';

    var rows = document.createElement('div');
    rows.className = 'lv-choose-rows';
    rank.forEach(function (kind) {
      var score = d.scores[kind] || 0;
      var row = document.createElement('label');
      row.className = 'lv-choose-row';
      row.innerHTML =
        '<input type="radio" name="lv-kind" value="' + esc(kind) + '"' +
        (kind === chosen ? ' checked' : '') + ' />' +
        '<span>' + esc(kindName(kind)) + '</span>' +
        '<span class="lv-meter"><i style="width:' + Math.round(score * 100) + '%"></i></span>' +
        '<span class="lv-score">' + ratio(score) + '</span>';
      row.querySelector('input').addEventListener('change', function () {
        state.kindChoice = kind;
      });
      rows.appendChild(row);
    });
    choose.appendChild(rows);

    var actions = document.createElement('div');
    actions.className = 'lv-choose-actions';
    var run = document.createElement('button');
    run.type = 'button';
    run.className = 'lv-btn lv-btn-primary';
    run.textContent = '解析并出报告';
    run.addEventListener('click', function () {
      runAnalysis(state.kindChoice || best);
    });
    actions.appendChild(run);

    var peek = document.createElement('button');
    peek.type = 'button';
    peek.className = 'lv-btn';
    peek.textContent = '先看头几行';
    peek.addEventListener('click', function () {
      previewFile();
    });
    actions.appendChild(peek);
    choose.appendChild(actions);

    var head = document.createElement('div');
    head.className = 'lv-report-head';
    head.innerHTML =
      '<div class="lv-report-title">' +
      '<h2>' + esc(d.name || state.target.name) + '</h2>' +
      (d.detected
        ? '<span class="lv-tag is-kind">' + esc(kindName(d.best)) + '</span>'
        : '<span class="lv-tag is-warn">认不出类型</span>') +
      '</div>' +
      '<p class="lv-report-sub">' +
      esc(bytes(d.size)) + ' · 读到 ' + num(scan.lines) + ' 行 / ' + esc(bytes(scan.bytes_read)) +
      (scan.truncated ? '（<strong>没读完：' + esc(scan.reason || '') + '</strong>）' : '') +
      (scan.encoding_suspect ? ' · <strong>编码可能不对，有替换字符</strong>' : '') +
      '</p>';

    var box = $('lv-report');
    box.innerHTML = '';
    box.appendChild(head);
    box.appendChild(choose);
    $('lv-stat').textContent = d.detected ? '把握 ' + ratio(d.confidence) : '需要手动指定类型';
  }

  function previewFile() {
    if (!state.target) return;
    request('/preview?root=' + encodeURIComponent(state.rootId) +
      '&path=' + encodeURIComponent(state.target.path) + '&lines=60')
      .then(function (data) {
        openDrawer(
          state.target.name + ' — 头几行',
          '共 ' + num(data.total_lines) + ' 行，文件 ' + bytes(data.file_size),
          '<pre class="lv-code-block">' + esc(renderRowTexts(data.rows)) + '</pre>'
        );
      })
      .catch(function (err) {
        window.alert(err.message);
      });
  }

  function renderRowTexts(rows) {
    return (rows || []).map(function (r) {
      return r.text;
    }).join('\n');
  }

  /* ------------------------------------------------------------ 出报告 */
  function runAnalysis(kind) {
    if (!kind || !state.target) return;
    state.busy = true;
    renderBusy('正在解析（大文件可能要几秒）…');
    request('/analyze', {
      method: 'POST',
      body: { root: state.rootId, path: state.target.path, kind: kind }
    }).then(function (data) {
      state.busy = false;
      if (data.detected === false && !data.file) {
        state.detect = {
          detected: false,
          scores: data.scores || {},
          best: '',
          confidence: 0,
          labels: data.labels || {},
          scan: data.scan || {},
          name: state.target.name,
          size: state.target.size
        };
        renderDetect();
        return;
      }
      state.report = data;
      renderReport();
    }).catch(function (err) {
      state.busy = false;
      renderError('解析失败：' + err.message);
    });
  }

  function renderReport() {
    var r = state.report;
    if (!r) return;
    var f = r.file || {};
    var scan = r.scan || {};

    var box = $('lv-report');
    box.innerHTML = '';

    // ---- 头部
    var head = document.createElement('div');
    head.className = 'lv-report-head';
    head.innerHTML =
      '<div class="lv-report-title">' +
      '<h2>' + esc(f.name || '') + '</h2>' +
      '<span class="lv-tag is-kind">' + esc(f.label || kindName(f.kind)) + '</span>' +
      (r.cached ? '<span class="lv-tag is-cached">来自缓存</span>' : '') +
      (scan.truncated ? '<span class="lv-tag is-warn">没读完</span>' : '') +
      '</div>' +
      '<p class="lv-report-sub">' +
      esc(f.path || '') + '<br />' +
      esc(bytes(f.size)) + ' · 修改于 ' + esc(fmtTime(f.mtime)) +
      ' · 解析耗时 ' + esc(r.elapsed != null ? r.elapsed + 's' : '—') +
      ' · 读到 ' + num(scan.lines) + ' 行（' + ratio(scan.coverage) + '）' +
      '</p>';
    box.appendChild(head);

    // ---- 提醒（没读完 / 编码可疑 / 解析器自己的 notes）
    var notes = (r.notes || []).slice();
    if (scan.truncated) {
      notes.unshift(
        '这份文件**没读完**：' + (scan.reason || '') +
        '。只读了 ' + bytes(scan.bytes_read) + ' / 共 ' + bytes(scan.file_size) +
        '，下面的数字只覆盖读到的部分。'
      );
    }
    if (scan.encoding_suspect) {
      notes.push('文件里出现了较多无法解码的字节（已替换为 □）。数字可能受此影响。');
    }
    if (notes.length) {
      var nb = document.createElement('div');
      nb.className = 'lv-notes';
      nb.innerHTML = notes.map(function (text) {
        return '<p>' + inlineCode(esc(text)) + '</p>';
      }).join('');
      box.appendChild(nb);
    }

    // ---- 各类型自己的报告体
    if (r.kind === 'nginx') renderNginx(box, r);
    else if (r.kind === 'java') renderJava(box, r);
    else if (r.kind === 'mysql') renderMysql(box, r);
    else {
      var unknown = document.createElement('p');
      unknown.className = 'lv-note';
      unknown.textContent = '这个类型还没有对应的报告视图。';
      box.appendChild(unknown);
    }
  }

  /* 把 `反引号` 包起来的那点文本渲染成 code。先 esc 过再调这个，所以是安全的。 */
  function inlineCode(text) {
    return text.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  }

  /* ------------------------------------------------------------ 共用零件 */

  /* KPI 条 */
  function kpis(items) {
    var wrap = document.createElement('div');
    wrap.className = 'lv-kpis';
    items.forEach(function (item) {
      if (!item) return;
      var cell = document.createElement('div');
      cell.className = 'lv-kpi';
      cell.innerHTML =
        '<span class="lv-kpi-label">' + esc(item.label) + '</span>' +
        '<span class="lv-kpi-value">' + esc(item.value) + '</span>' +
        (item.sub ? '<span class="lv-kpi-sub">' + esc(item.sub) + '</span>' : '');
      wrap.appendChild(cell);
    });
    return wrap;
  }

  /* 区块标题 */
  function block(title, hint) {
    var wrap = document.createElement('div');
    wrap.className = 'lv-block';
    var head = document.createElement('div');
    head.className = 'lv-block-head';
    head.innerHTML = '<h3>' + esc(title) + '</h3>' + (hint ? '<p>' + esc(hint) + '</p>' : '');
    wrap.appendChild(head);
    return wrap;
  }

  /* 时间直方图。桶点击不下钻 —— 直方图的时间范围不等于某一行。 */
  function histogram(tl) {
    var wrap = document.createElement('div');
    if (!tl || !tl.buckets || !tl.buckets.length) {
      wrap.innerHTML = '<p class="lv-note">这份日志里没有认得出的时间戳，画不出时间分布。</p>';
      return wrap;
    }
    var max = 0;
    tl.buckets.forEach(function (b) {
      if (b.n > max) max = b.n;
    });
    var chart = document.createElement('div');
    chart.className = 'lv-hist';
    var bars = [];
    tl.buckets.forEach(function (b) {
      var bar = document.createElement('div');
      bar.className = 'lv-bar' + (b.n ? '' : ' is-zero');
      bar.style.height = (b.n ? Math.max(3, Math.round((b.n / (max || 1)) * 100)) : 0) + '%';
      bar.title = b.t + '  ' + num(b.n) + ' 条';
      chart.appendChild(bar);
      bars.push(b.t + '=' + b.n);
    });
    wrap.appendChild(chart);
    var foot = document.createElement('div');
    foot.className = 'lv-hist-foot';
    foot.innerHTML = '<span>' + esc(tl.buckets[0].t) + '</span>' +
      '<span>每格 ' + esc(tl.step_label) + ' · 共 ' + num(tl.total) + ' 条带时间戳的记录</span>' +
      '<span>' + esc(tl.buckets[tl.buckets.length - 1].t) + '</span>';
    wrap.appendChild(foot);
    return wrap;
  }

  /* 可下钻的表格。
   *
   * `spec`: { columns: [{name, label, align, cls}], rows: [{cells:[...], drill:{field,value}|drillLine:n}] }
   * 一行有 drill 就是可点的 —— 点开抽屉，捞原始行。
   */
  function drillTable(spec) {
    var table = document.createElement('table');
    table.className = 'lv-table';

    // 有没有可下钻的行，决定要不要多一列。**在画表头之前先问清楚** ——
    // 画完再往 thead 里补一格的话，「有没有这一列」在表头和数据行两边
    // 各判一次，两处一旦不一致表格就错位，而且是那种看着"只是没对齐"的错。
    var anyDrill = spec.rows.some(function (row) {
      return !!(row.drill || row.drillLine);
    });
    var tail = anyDrill ? '<th></th>' : '';

    var thead = document.createElement('thead');
    thead.innerHTML = '<tr>' + spec.columns.map(function (col) {
      return '<th' + (col.align === 'right' ? ' class="lv-num"' : '') + '>' + esc(col.label) + '</th>';
    }).join('') + tail + '</tr>';
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    spec.rows.forEach(function (row) {
      var tr = document.createElement('tr');
      var clickable = !!(row.drill || row.drillLine);
      if (clickable) tr.className = 'is-clickable';
      tr.innerHTML = spec.columns.map(function (col, index) {
        var raw = row.cells[index];
        var value = raw == null ? '' : raw;
        var cls = [];
        if (col.align === 'right') cls.push('lv-num');
        if (col.cls) cls.push(col.cls);
        return '<td' + (cls.length ? ' class="' + cls.join(' ') + '"' : '') + '>' + value + '</td>';
      }).join('') +
        (anyDrill ? '<td class="lv-drill-mark">' + (clickable ? '看原始行 ›' : '') + '</td>' : '');
      if (clickable) {
        tr.addEventListener('click', function () {
          if (row.drillLine) {
            openDrill({ field: 'line', value: String(row.drillLine) }, row.drillTitle || '');
          } else {
            openDrill(row.drill, row.drillTitle || '');
          }
        });
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  /* ------------------------------------------------------------ nginx */
  function renderNginx(box, r) {
    var t = r.totals || {};
    box.appendChild(kpis([
      { label: '请求总数', value: num(t.requests), sub: '解析成功 ' + num(t.parsed) + ' 行' },
      { label: '回给客户端的流量', value: bytes(t.bytes) },
      { label: '没认出的行', value: num(t.unparsed), sub: t.unparsed ? '已在提醒里列出前几行' : '全部认出来了' },
      {
        label: '最慢的一次请求',
        value: r.max_request_time != null ? r.max_request_time.toFixed(3) + 's' : '—',
        sub: r.max_request_time != null ? '' : '日志里没有 $request_time'
      }
    ]));

    box.appendChild(histogram(r.timeline));

    // 状态码
    if ((r.status_codes || []).length) {
      var sb = block('状态码分布', '点一行看这类响应的原始日志');
      sb.appendChild(drillTable({
        columns: [
          { name: 'code', label: '状态码' },
          { name: 'group', label: '分组' },
          { name: 'count', label: '次数', align: 'right' },
          { name: 'bytes', label: '回包字节', align: 'right' }
        ],
        rows: r.status_codes.map(function (item) {
          var bad = String(item.code).charAt(0) === '5';
          var warn = String(item.code).charAt(0) === '4';
          var pillClass = 'lv-pill' + (bad ? ' is-bad' : warn ? ' is-warn' : ' is-ok');
          return {
            cells: [
              '<span class="' + pillClass + '">' + esc(item.code) + '</span>',
              esc(item.group),
              num(item.count),
              bytes(item.bytes)
            ],
            // 锚点是"这个状态码第一次出现的行号"；没有锚点时退回按状态码扫
            drillLine: item.anchor_line || 0,
            drill: { field: 'status', value: String(item.code) },
            drillTitle: '状态码 ' + item.code
          };
        })
      }));
      box.appendChild(sb);
    }

    // 方法
    if ((r.methods || []).length) {
      var mb = block('请求方法');
      mb.appendChild(drillTable({
        columns: [{ name: 'name', label: '方法' }, { name: 'count', label: '次数', align: 'right' }],
        rows: r.methods.map(function (item) {
          return { cells: [esc(item.name), num(item.count)], drill: null };
        })
      }));
      box.appendChild(mb);
    }

    box.appendChild(topBlock('被调得最多的接口', r.top_urls, 'url', 'url', '次'));
    box.appendChild(topBlock('来源 IP', r.top_ips, 'ip', 'ip', '次'));
    box.appendChild(topBlock('客户端（UA）', r.top_uas, 'ua', 'ua', '次'));

    // 慢请求：榜单按耗时排（后端 to_ranked），所以这一列的值比次数有意义
    if ((r.slow_requests || []).length) {
      var slow = block('最慢的请求', '按单次耗时排（不是按次数）');
      slow.appendChild(drillTable({
        columns: [
          { name: 'request', label: '请求', cls: 'lv-code' },
          { name: 'metric', label: '最慢一次', align: 'right' },
          { name: 'count', label: '出现次数', align: 'right' }
        ],
        rows: r.slow_requests.map(function (item) {
          return {
            cells: [
              esc(item.label || item.request),
              item.metric != null ? item.metric.toFixed(3) + 's' : '—',
              num(item.count)
            ],
            drill: { field: 'request', value: item.request },
            drillTitle: item.request
          };
        })
      }));
      box.appendChild(slow);
    }
  }

  /* Top 列表的通用块（nginx 与 java 都用） */
  function topBlock(title, items, keyName, field, unit) {
    var b = block(title);
    if (!items || !items.length) {
      b.innerHTML += '<p class="lv-note">这一项没有数据。</p>';
      return b;
    }
    b.appendChild(drillTable({
      columns: [
        { name: keyName, label: title, cls: 'lv-code' },
        { name: 'count', label: unit || '次数', align: 'right' }
      ],
      rows: items.map(function (item) {
        var value = item[keyName] != null ? item[keyName] : item.value;
        return {
          cells: [esc(value), num(item.count)],
          drill: field ? { field: field, value: value } : null,
          drillLine: 0,
          drillTitle: String(value)
        };
      })
    }));
    return b;
  }

  /* ------------------------------------------------------------ java */
  function renderJava(box, r) {
    var t = r.totals || {};
    var levels = r.levels || [];
    var errCount = 0;
    levels.forEach(function (item) {
      if (item.name === 'ERROR' || item.name === 'FATAL' || item.name === 'SEVERE' || item.name === 'CRITICAL') {
        errCount += item.count;
      }
    });
    var warnCount = 0;
    levels.forEach(function (item) {
      if (item.name === 'WARN') warnCount += item.count;
    });

    box.appendChild(kpis([
      { label: '日志条数', value: num(t.entries), sub: '已把异常栈合并回主记录' },
      { label: 'ERROR / FATAL', value: num(errCount), sub: errCount ? '在最重的那一档' : '没有严重错误' },
      { label: 'WARN', value: num(warnCount) },
      { label: '栈行', value: num(t.stack_lines), sub: '被合并进来的续行' }
    ]));

    box.appendChild(histogram(r.timeline));

    // 级别构成
    if (levels.length) {
      var lb = block('级别构成', '点一行看这个级别的原始日志');
      lb.appendChild(drillTable({
        columns: [
          { name: 'name', label: '级别' },
          { name: 'count', label: '条数', align: 'right' }
        ],
        rows: levels.map(function (item) {
          var cls = 'lv-pill';
          if (item.name === 'ERROR' || item.name === 'FATAL' || item.name === 'SEVERE' || item.name === 'CRITICAL') cls += ' is-bad';
          else if (item.name === 'WARN') cls += ' is-warn';
          else if (item.name === 'INFO') cls += ' is-ok';
          return {
            cells: ['<span class="' + cls + '">' + esc(item.name) + '</span>', num(item.count)],
            drill: { field: 'level', value: item.name },
            drillTitle: '级别 ' + item.name
          };
        })
      }));
      box.appendChild(lb);
    }

    // 异常类型。这是 Java 报告里最值钱的一段
    if ((r.top_exceptions || []).length) {
      var eb = block('异常 Top', '已按类型归并（同名的自定义类会排在一起）');
      eb.appendChild(drillTable({
        columns: [
          { name: 'exception', label: '异常类型', cls: 'lv-code' },
          { name: 'count', label: '次数', align: 'right' }
        ],
        rows: r.top_exceptions.map(function (item) {
          return {
            cells: [esc(item.exception), num(item.count)],
            drill: { field: 'exception', value: item.exception },
            drillTitle: item.exception
          };
        })
      }));
      box.appendChild(eb);
    }

    // 消息归并：这是"相似错误归并"那一层
    if ((r.top_messages || []).length) {
      var msb = block('消息 Top（相似归并后）', '时间戳/UUID/数字已抹成占位符，只差 ID 的同一件事算一条');
      msb.appendChild(drillTable({
        columns: [
          { name: 'message', label: '消息指纹', cls: 'lv-code' },
          { name: 'count', label: '次数', align: 'right' }
        ],
        rows: r.top_messages.map(function (item) {
          return {
            cells: [esc(item.message), num(item.count)],
            drill: { field: 'message', value: item.message },
            drillTitle: item.message
          };
        })
      }));
      box.appendChild(msb);
    }

    if ((r.top_loggers || []).length) {
      var gb = block('Logger Top', '点一行看这个 logger 的原始日志');
      gb.appendChild(drillTable({
        columns: [
          { name: 'logger', label: 'logger', cls: 'lv-code' },
          { name: 'count', label: '次数', align: 'right' }
        ],
        rows: r.top_loggers.map(function (item) {
          return {
            cells: [esc(item.logger), num(item.count)],
            drill: { field: 'logger', value: item.logger },
            drillTitle: item.logger
          };
        })
      }));
      box.appendChild(gb);
    }

    // 错误按小时
    if ((r.errors_by_hour || []).length) {
      var hb = block('错误发生时间', '每小时多少条 ERROR 及以上');
      hb.appendChild(drillTable({
        columns: [
          { name: 'hour', label: '时间', cls: 'lv-code' },
          { name: 'count', label: '条数', align: 'right' }
        ],
        rows: r.errors_by_hour.map(function (item) {
          return { cells: [esc(item.hour), num(item.count)], drill: null };
        })
      }));
      box.appendChild(hb);
    }
  }

  /* ------------------------------------------------------------ mysql */
  function renderMysql(box, r) {
    var t = r.totals || {};
    var ratioTotal = (t.rows_sent_total || 0) > 0
      ? (t.rows_examined_total / t.rows_sent_total)
      : null;

    box.appendChild(kpis([
      { label: '慢查询条数', value: num(t.slow_queries) },
      { label: '总耗时', value: (t.query_time_total || 0).toFixed(3) + 's' },
      { label: '平均耗时', value: (t.avg_query_time || 0).toFixed(4) + 's' },
      {
        label: '扫描 / 返回',
        value: ratioTotal != null ? Math.round(ratioTotal) + '×' : '—',
        sub: '越大越可能是没走索引'
      }
    ]));

    box.appendChild(histogram(r.timeline));

    // 这几个榜都是按度量排的，所以把度量单列一列出来
    mysqlRank(box, '最慢的 SQL', r.slowest, function (item) {
      return item.metric != null ? item.metric.toFixed(3) + 's' : '—';
    }, '单次耗时', 'sql');
    mysqlRank(box, '扫描行数 Top', r.full_scans, function (item) {
      return item.metric != null ? num(Math.round(item.metric)) + ' 行' : '—';
    }, '扫描行数', 'sql');
    mysqlRank(box, '锁等待 Top', r.locks, function (item) {
      return item.metric != null ? item.metric.toFixed(3) + 's' : '—';
    }, '锁时间', 'sql');
    mysqlRank(box, '扫描/返回 比例最差', r.worst_ratio, function (item) {
      return item.metric != null ? Math.round(item.metric) + '×' : '—';
    }, '比例', 'sql');

    // 这两个按次数排（"谁制造得最多"就是要按次数）
    if ((r.databases || []).length) {
      var db = block('库维度', '哪个库在制造慢查询');
      db.appendChild(drillTable({
        columns: [{ name: 'db', label: '库' }, { name: 'count', label: '条数', align: 'right' }],
        rows: r.databases.map(function (item) {
          return {
            cells: [esc(item.db), num(item.count)],
            drill: { field: 'db', value: item.db },
            drillTitle: '库 ' + item.db
          };
        })
      }));
      box.appendChild(db);
    }

    if ((r.users || []).length) {
      var ub = block('账号维度', '哪个账号在制造慢查询');
      ub.appendChild(drillTable({
        columns: [{ name: 'user', label: '账号' }, { name: 'count', label: '条数', align: 'right' }],
        rows: r.users.map(function (item) {
          return { cells: [esc(item.user), num(item.count)], drill: null };
        })
      }));
      box.appendChild(ub);
    }
  }

  function mysqlRank(box, title, items, metricText, metricLabel, field) {
    if (!items || !items.length) return;
    var b = block(title, '按' + metricLabel + '排（不是按出现次数）');
    b.appendChild(drillTable({
      columns: [
        { name: 'sql', label: 'SQL 模板', cls: 'lv-code' },
        { name: 'metric', label: metricLabel, align: 'right' },
        { name: 'count', label: '出现次数', align: 'right' }
      ],
      rows: items.map(function (item) {
        return {
          cells: [esc(item.label || item.sql), metricText(item), num(item.count)],
          drill: { field: field, value: item.sql },
          drillTitle: item.sql
        };
      })
    }));
    box.appendChild(b);
  }

  /* ------------------------------------------------------------ 抽屉 */
  function openDrawer(title, sub, html) {
    $('lv-drawer-name').textContent = title;
    $('lv-drawer-sub').textContent = sub || '';
    $('lv-drawer-body').innerHTML = html;
    $('lv-drawer').hidden = false;
  }

  function closeDrawer() {
    $('lv-drawer').hidden = true;
    $('lv-drawer-body').innerHTML = '';
  }

  function openDrill(drill, title) {
    if (!drill || !state.target) return;
    openDrawer(title || '原始行', '正在读取原始行…', '<p class="lv-note">正在读取…</p>');
    var params = new URLSearchParams();
    params.set('root', state.rootId);
    params.set('path', state.target.path);
    params.set('kind', (state.report && state.report.file && state.report.file.kind) || state.kindChoice || '');
    params.set('field', drill.field);
    params.set('value', drill.value);
    params.set('limit', '300');

    request('/drill?' + params.toString()).then(function (data) {
      renderDrill(drill, title, data);
    }).catch(function (err) {
      openDrawer(title || '原始行', '', '<p class="lv-error">' + esc(err.message) + '</p>');
    });
  }

  function renderDrill(drill, title, data) {
    var rows = data.rows || [];
    var sub = '命中 ' + num(data.matched) + ' 行（扫了 ' + num(data.scanned) + ' 行）';
    if (data.note) sub += ' · ' + data.note;
    if (data.capped) sub += ' · 只显示前 ' + num(data.limit) + ' 行';
    if (data.truncated) sub += ' · 注意：这份文件当时没读完（' + (data.reason || '') + '）';

    var body;
    if (!rows.length) {
      body = '<p class="lv-note">' + esc(data.note || '没有取到原始行。') + '</p>';
    } else {
      body = '<div class="lv-rows">' + rows.map(function (row) {
        return '<div class="lv-rowline' + (row.hit ? ' is-hit' : '') + '">' +
          '<span class="lv-rowno">' + num(row.line) + '</span>' +
          '<span class="lv-rowtext">' + esc(row.text) + '</span>' +
          '</div>';
      }).join('') + '</div>';
    }
    if (drill && drill.field && drill.field !== 'line') {
      sub = '“' + String(drill.value).slice(0, 80) + '” · ' + sub;
    }
    openDrawer(title || '原始行', sub, body);
  }

  /* ------------------------------------------------------------ 缓存 */
  function openCache() {
    request('/cache').then(function (data) {
      $('lv-modal-body').innerHTML =
        '<dl>' +
        '<dt>缓存条数</dt><dd>' + num(data.entries) + ' / ' + num(data.max_entries) + '</dd>' +
        '<dt>占用</dt><dd>' + bytes(data.bytes) + '</dd>' +
        '<dt>库文件</dt><dd>' + esc(data.db_path || '—') + '</dd>' +
        '</dl>' +
        '<p class="lv-note">缓存里存的是分析结果，不是日志正文。' +
        '日志文件一变（大小或修改时间），对应的缓存自动作废。</p>' +
        (data.error ? '<p class="lv-error">' + esc(data.error) + '</p>' : '');
      $('lv-modal').hidden = false;
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  function clearCache() {
    if (!window.confirm('清空分析缓存？\n\n只是让下次打开重新分析一遍，磁盘上的日志文件不会被改动。')) return;
    request('/cache', { method: 'DELETE' }).then(function (data) {
      window.alert(data.note || ('已清空 ' + data.removed + ' 条。'));
      $('lv-modal').hidden = true;
      if (state.target) {
        // 缓存没了，手上的报告就过期了 —— 直接重跑一遍
        runAnalysis((state.report && state.report.file && state.report.file.kind) || state.kindChoice);
      }
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  /* ------------------------------------------------------------ 事件 */
  function bind() {
    $('lv-add-toggle').addEventListener('click', function () {
      var form = $('lv-add');
      form.hidden = !form.hidden;
      if (!form.hidden) $('lv-add-path').focus();
    });

    $('lv-add-cancel').addEventListener('click', function () {
      $('lv-add').hidden = true;
      $('lv-add-msg').textContent = '';
    });

    $('lv-add').addEventListener('submit', function (event) {
      event.preventDefault();
      var msg = $('lv-add-msg');
      var pathInput = $('lv-add-path');
      msg.textContent = '';
      request('/roots', {
        method: 'POST',
        body: { path: pathInput.value, name: $('lv-add-name').value }
      }).then(function (data) {
        pathInput.value = '';
        $('lv-add-name').value = '';
        $('lv-add').hidden = true;
        state.rootId = data.item.id;
        state.path = '';
        return loadRoots(false);
      }).catch(function (err) {
        msg.textContent = err.message;
      });
    });

    $('lv-search').addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      state.search = event.target.value.trim();
      loadDir(state.path);
    });
    $('lv-search').addEventListener('search', function (event) {
      if (!event.target.value && state.search) {
        state.search = '';
        loadDir(state.path);
      }
    });

    $('lv-cache').addEventListener('click', openCache);
    $('lv-modal-close').addEventListener('click', function () {
      $('lv-modal').hidden = true;
    });
    $('lv-modal-mask').addEventListener('click', function () {
      $('lv-modal').hidden = true;
    });
    $('lv-cache-clear').addEventListener('click', clearCache);

    $('lv-drawer-close').addEventListener('click', closeDrawer);
    $('lv-drawer-mask').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!$('lv-drawer').hidden) closeDrawer();
      else if (!$('lv-modal').hidden) $('lv-modal').hidden = true;
    });
  }

  window.addEventListener('DOMContentLoaded', function () {
    bind();
    loadRoots(false).catch(function (err) {
      $('lv-filelist').innerHTML = '<p class="lv-error">读取目录名单失败：' + esc(err.message) + '</p>';
    });
  });
})();
