/* 本地文件列表 —— 模块自带脚本。
 *
 * 无构建、无框架、无外部依赖：改完直接刷 iframe 就生效。
 *
 * 结构很简单：左边书库，右边一本「书的目录」。目录按层懒加载 ——
 * 点一下展开一层，不预取整棵子树（深层目录可能有几万条，一次读完
 * 既慢又没意义）。
 *
 * 编号规则：**目录才是章**，同级第 n 个目录编成 n，它的子目录就是 n.1、n.2…
 * 文件不编号，作为叶子条目排在同级目录后面 —— 这正是书目录的样子：
 * 章节有序号，附录和页码列表没有。
 *
 * 所有请求都带同源 Cookie（中台在首次进入模块时种下的模块票据），
 * 所以这里不需要、也拿不到中台令牌。
 */
(function () {
  'use strict';

  var MOUNT = (window.__FL__ && window.__FL__.mount) || '/filelist';
  var API = MOUNT + '/api';

  var state = {
    roots: [],
    rootId: '',
    rootName: '',
    rootPath: '',
    path: '',          // 当前聚焦的相对路径（'' = 书库根）
    showFiles: true,
    counts: true,
    depth: 2,
    sort: 'name',
    search: '',
    // rel -> { open, loaded, childrenEl, size }
    nodes: {}
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

  function bytes(n) {
    if (n == null) return '';
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

  function count(n) {
    if (n == null) return '';
    return n >= 5000 ? '5000+' : n + ' 条';
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

  /* ------------------------------------------------------------ 书库 */
  function loadRoots(keepSelection) {
    return request('/roots').then(function (data) {
      state.roots = data.items || [];
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
        drawSheet();
      }
    });
  }

  function drawRoots() {
    var list = $('fl-roots');
    list.innerHTML = '';
    if (!state.roots.length) {
      var li = document.createElement('li');
      li.className = 'fl-root';
      li.innerHTML = '<div class="fl-root-body"><span class="fl-root-path">还没有登记书库</span></div>';
      list.appendChild(li);
      return;
    }
    state.roots.forEach(function (root) {
      var li = document.createElement('li');
      li.className = 'fl-root' + (root.id === state.rootId ? ' is-active' : '') +
        (root.exists ? '' : ' is-missing');
      li.innerHTML =
        '<div class="fl-root-body">' +
        '<span class="fl-root-name">' + esc(root.name) + '</span>' +
        '<span class="fl-root-path" title="' + esc(root.path) + '">' + esc(root.path) + '</span>' +
        (root.exists ? '' : '<span class="fl-root-flag">目录不可访问（盘没挂上或被改名了）</span>') +
        '</div>' +
        '<button type="button" class="fl-root-x" title="移出书库（不动磁盘）">×</button>';
      li.addEventListener('click', function (event) {
        if (event.target.classList.contains('fl-root-x')) return;
        selectRoot(root.id);
      });
      li.querySelector('.fl-root-x').addEventListener('click', function (event) {
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
    state.rootPath = root.path;
    state.path = '';
    state.nodes = {};
    drawRoots();
    drawSheet();
  }

  function removeRoot(root) {
    if (!window.confirm('把「' + root.name + '」移出书库？\n\n只会取消登记，磁盘上的目录不会被改动。')) {
      return;
    }
    request('/roots/' + encodeURIComponent(root.id), { method: 'DELETE' }).then(function () {
      if (state.rootId === root.id) {
        state.rootId = '';
        state.path = '';
      }
      return loadRoots();
    }).catch(function (err) {
      window.alert(err.message);
    });
  }

  /* ------------------------------------------------------------ 目录页 */
  function drawSheet() {
    var sheet = $('fl-sheet');
    var empty = $('fl-empty');
    var stat = $('fl-stat');

    if (!state.rootId) {
      sheet.innerHTML = '';
      sheet.appendChild(empty);
      empty.hidden = false;
      stat.textContent = '';
      drawCrumbs();
      return;
    }

    sheet.innerHTML = '';
    var volume = document.createElement('p');
    volume.className = 'fl-volume';
    volume.textContent = '卷 · ' + state.rootName + '  ' + state.rootPath +
      (state.path ? '  →  ' + state.path : '');
    sheet.appendChild(volume);

    var ul = document.createElement('ul');
    ul.className = 'fl-toc';
    sheet.appendChild(ul);
    drawCrumbs();
    loadLevel(state.path, '', ul, state.depth);
  }

  function drawCrumbs() {
    var box = $('fl-crumbs');
    box.innerHTML = '';
    if (!state.rootId) return;

    var parts = state.path ? state.path.split('/') : [];
    var mk = function (label, target) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'fl-crumb';
      b.textContent = label;
      b.addEventListener('click', function () {
        focusPath(target);
      });
      return b;
    };

    box.appendChild(mk(state.rootName, ''));
    var acc = [];
    parts.forEach(function (piece) {
      acc.push(piece);
      var sep = document.createElement('span');
      sep.className = 'fl-crumb-sep';
      sep.textContent = '/';
      box.appendChild(sep);
      box.appendChild(mk(piece, acc.join('/')));
    });
  }

  function focusPath(rel) {
    state.path = rel || '';
    state.nodes = {};
    drawSheet();
  }

  function nodeOf(rel) {
    if (!state.nodes[rel]) {
      state.nodes[rel] = { open: false, loaded: false, el: null, size: null };
    }
    return state.nodes[rel];
  }

  function loadLevel(rel, prefix, ul, autoDepth) {
    var node = nodeOf(rel);
    ul.innerHTML = '';
    var waiting = document.createElement('li');
    waiting.className = 'fl-note';
    waiting.textContent = '正在读取…';
    ul.appendChild(waiting);

    request('/entries?' + query({ root: state.rootId, path: rel })).then(function (data) {
      node.loaded = true;
      // 把结果留在节点上：收起再展开、或整页重画时可以直接复用，
      // 不必为一个刚看过的目录再发一次请求
      node.data = data;
      drawLevel(rel, prefix, ul, data, autoDepth);
    }).catch(function (err) {
      ul.innerHTML = '';
      var li = document.createElement('li');
      li.className = 'fl-error';
      li.textContent = '读不了这一层：' + err.message;
      ul.appendChild(li);
    });
  }

  function query(extra) {
    var params = new URLSearchParams();
    params.set('show_files', state.showFiles ? 'true' : 'false');
    params.set('sort', state.sort);
    if (state.search) params.set('search', state.search);
    Object.keys(extra || {}).forEach(function (key) {
      params.set(key, extra[key]);
    });
    return params.toString();
  }

  function drawLevel(rel, prefix, ul, data, autoDepth) {
    ul.innerHTML = '';

    if (!data.entries.length) {
      var blank = document.createElement('li');
      blank.className = 'fl-note';
      blank.textContent = state.search ? '本层没有匹配「' + state.search + '」的条目' : '（空）';
      ul.appendChild(blank);
      updateStat();
      return;
    }

    var dirIndex = 0;
    data.entries.forEach(function (item) {
      var li = document.createElement('li');
      var row = document.createElement('div');
      row.className = 'fl-row ' + (item.is_dir ? 'is-dir' : 'is-file');

      var num = '';
      var childPrefix = '';
      if (item.is_dir) {
        dirIndex += 1;
        num = prefix + dirIndex;
        childPrefix = num + '.';
      }

      var badge = '';
      var badgeClass = 'fl-badge';
      if (item.outside) {
        // 指向书库之外的链接。服务端连它的条目数都不数，这里也不要装作能看。
        badge = '外部链接';
        badgeClass += ' is-warn';
      } else if (item.is_dir) {
        if (state.counts && item.child_count != null) badge = count(item.child_count);
      } else if (item.ext) {
        badge = item.ext;
      }

      var node = item.is_dir ? nodeOf(item.path) : null;
      var meta = '';
      if (item.outside) meta = '不可访问';
      else if (node && node.size != null) meta = bytes(node.size);
      else if (!item.is_dir) meta = bytes(item.size);
      if (item.unreadable) meta = '读不了';

      row.innerHTML =
        '<span class="fl-caret' + (item.is_dir ? '' : ' is-leaf') + '">▶</span>' +
        '<span class="fl-num">' + (item.is_dir ? esc(num) : '·') + '</span>' +
        '<span class="fl-name" title="' + esc(item.name) + '">' + esc(item.name) + '</span>' +
        '<span class="fl-leader"></span>' +
        (badge ? '<span class="' + badgeClass + '">' + esc(badge) + '</span>' : '') +
        '<span class="fl-meta">' + esc(meta) + '</span>' +
        (item.is_dir
          ? '<span class="fl-actions">' +
            (item.outside
              ? ''
              : '<button type="button" class="fl-mini" data-act="size">算厚度</button>') +
            '<button type="button" class="fl-mini" data-act="focus">进入</button>' +
            '</span>'
          : '');

      li.appendChild(row);

      if (item.is_dir) {
        if (node.open) row.classList.add('is-open');
        var children = document.createElement('ul');
        children.className = 'fl-children';
        children.hidden = !node.open;
        li.appendChild(children);
        node.el = children;
        // 之前已经展开过的，重新画时直接把内容填回去
        if (node.open && node.data) {
          drawLevel(item.path, childPrefix, children, node.data, 0);
        }
        if (node.open && !node.data) {
          loadLevel(item.path, childPrefix, children, 0);
        }

        row.addEventListener('click', function (event) {
          var act = event.target.getAttribute && event.target.getAttribute('data-act');
          if (act === 'focus') {
            event.stopPropagation();
            focusPath(item.path);
            return;
          }
          if (act === 'size') {
            event.stopPropagation();
            measure(row, item);
            return;
          }
          toggle(item.path, childPrefix, row, children);
        });
      }

      ul.appendChild(li);
    });

    updateStat();
    if (autoDepth > 0) autoExpand(ul, autoDepth);
  }

  function toggle(rel, prefix, row, children) {
    var node = nodeOf(rel);
    if (node.open) {
      node.open = false;
      row.classList.remove('is-open');
      children.hidden = true;
      return;
    }
    node.open = true;
    row.classList.add('is-open');
    children.hidden = false;
    if (node.loaded && node.data) {
      drawLevel(rel, prefix, children, node.data, 0);
    } else {
      loadLevel(rel, prefix, children, 0);
    }
  }

  function autoExpand(ul, remaining) {
    var rows = ul.querySelectorAll(':scope > li > .fl-row.is-dir');
    Array.prototype.forEach.call(rows, function (row) {
      var children = row.parentElement.querySelector('.fl-children');
      if (children && children.hidden) {
        row.click();
      }
    });
    if (remaining > 1) {
      // 下一层的子列表是异步填进来的，等一拍再展开
      window.setTimeout(function () {
        Array.prototype.slice.call(ul.querySelectorAll('.fl-children')).forEach(function (child) {
          if (!child.hidden) autoExpand(child, remaining - 1);
        });
      }, 90);
    }
  }

  function collapseAll() {
    Object.keys(state.nodes).forEach(function (key) {
      state.nodes[key].open = false;
    });
    drawSheet();
  }

  function measure(row, item) {
    var meta = row.querySelector('.fl-meta');
    meta.textContent = '计算中…';
    var url = '/size?' + query({ root: state.rootId, path: item.path });
    request(url).then(function (data) {
      var node = nodeOf(item.path);
      node.size = data.bytes;
      meta.textContent = bytes(data.bytes) + (data.truncated ? ' 以上' : '');
      meta.title = (data.truncated ? '未走完（有预算上限）：' : '') +
        data.dirs + ' 个目录 / ' + data.files + ' 个文件' +
        (data.errors ? '，' + data.errors + ' 处读不了' : '');
    }).catch(function (err) {
      meta.textContent = '算不了';
      meta.title = err.message;
    });
  }

  function updateStat() {
    var stat = $('fl-stat');
    if (!state.rootId) {
      stat.textContent = '';
      return;
    }
    stat.textContent = (state.search ? '筛选「' + state.search + '」 · ' : '') +
      '已展开 ' + Object.keys(state.nodes).filter(function (k) {
        return state.nodes[k].open;
      }).length + ' 层';
  }

  /* ------------------------------------------------------------ 事件 */
  function bind() {
    $('fl-show-files').addEventListener('change', function (event) {
      state.showFiles = event.target.checked;
      state.nodes = {};
      drawSheet();
    });

    $('fl-counts').addEventListener('change', function (event) {
      state.counts = event.target.checked;
      state.nodes = {};
      drawSheet();
    });

    $('fl-depth').addEventListener('change', function (event) {
      state.depth = parseInt(event.target.value, 10) || 1;
      state.nodes = {};
      drawSheet();
    });

    $('fl-sort').addEventListener('change', function (event) {
      state.sort = event.target.value;
      state.nodes = {};
      drawSheet();
    });

    $('fl-collapse').addEventListener('click', collapseAll);

    $('fl-search').addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      state.search = event.target.value.trim();
      state.nodes = {};
      drawSheet();
    });
    $('fl-search').addEventListener('search', function (event) {
      if (!event.target.value) {
        state.search = '';
        state.nodes = {};
        drawSheet();
      }
    });

    $('fl-add-toggle').addEventListener('click', function () {
      var form = $('fl-add');
      form.hidden = !form.hidden;
      if (!form.hidden) $('fl-add-path').focus();
    });
    $('fl-add-cancel').addEventListener('click', function () {
      $('fl-add').hidden = true;
      $('fl-add-msg').textContent = '';
    });
    $('fl-add').addEventListener('submit', function (event) {
      event.preventDefault();
      var pathInput = $('fl-add-path');
      var msg = $('fl-add-msg');
      msg.textContent = '';
      request('/roots', {
        method: 'POST',
        body: { path: pathInput.value, name: $('fl-add-name').value }
      }).then(function (data) {
        pathInput.value = '';
        $('fl-add-name').value = '';
        $('fl-add').hidden = true;
        state.rootId = data.item.id;
        state.path = '';
        return loadRoots(false);
      }).catch(function (err) {
        msg.textContent = err.message;
      });
    });
  }

  window.addEventListener('DOMContentLoaded', function () {
    bind();
    loadRoots(false).catch(function (err) {
      $('fl-empty').textContent = '读取书库失败：' + err.message;
    });
  });
})();
