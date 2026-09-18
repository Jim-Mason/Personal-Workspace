/* ------------------------------------------------------------------
 * 中台反向代理注入的「路径垫片」
 *
 * 为什么需要它：
 * 模块被挂在 /opsgen/ 这样的子路径下，但它自己以为住在根路径。
 * 页面里的绝对路径（/static/x.css、/api/preview）如果原样发出去，
 * 就会打到中台的根路径上去 —— 要么 404，要么更糟：撞上中台自己的
 * /api/* 接口。
 *
 * 静态改写（对 HTML 属性、CSS url()）能解决绝大部分，但解决不了
 * 运行时才算出来的路径（`fetch('/api/favorites/' + name)`、
 * `location.href = x`）。那些只能在这一层拦。
 *
 * 另外两件事也放这里：
 *   - 实时通道被中台阻断时，给一个会说人话的替身，而不是让页面
 *     静默地重连失败、用户反复点「执行」却没反应
 *   - 外部 CDN 被剥掉后，给 highlight.js 一个空实现，避免
 *     后续脚本因为 hljs 未定义而中断
 * ------------------------------------------------------------------ */
(function () {
  'use strict';

  var CFG = window.__LOCALDECK__ || {};
  var MOUNT = CFG.mount || '';
  if (!MOUNT) return;

  /* ------------------------------------------------ 1. 实时通道替身 */
  if (CFG.realtime === false) {
    var makeStub = function () {
      var handlers = {};
      var api = {
        connected: false,
        disconnected: true,
        id: null,
        nsp: '/',
        io: {},
        on: function (ev, cb) {
          (handlers[ev] = handlers[ev] || []).push(cb);
          return api;
        },
        once: function (ev, cb) {
          return api.on(ev, cb);
        },
        off: function (ev) {
          if (ev) delete handlers[ev];
          else handlers = {};
          return api;
        },
        emit: function (ev) {
          if (ev !== 'execute_script') return api;
          var text =
            '中台未放行本模块的实时通道，「在线执行脚本」已停用。\n' +
            '\n' +
            '原因：这一能力的实质是在本机执行任意脚本，等同于一个\n' +
            '任何人都能通过浏览器触发的远程命令执行入口。中台的\n' +
            '安全边界里明确写了「不提供任意命令执行」。\n' +
            '\n' +
            '要开启：在 modules/<id>/module.py 里打开 allow_realtime，\n' +
            '并自行配好白名单 + 逐次确认 + 参数模板。\n' +
            '\n' +
            '脚本本身已经生成好了 —— 请复制到目标服务器上执行。\n';
          (handlers.execution_output || []).forEach(function (cb) {
            cb({ type: 'error', data: text });
          });
          (handlers.execution_done || []).forEach(function (cb) {
            cb({ code: 126 });
          });
          return api;
        },
        disconnect: function () {
          return api;
        },
        close: function () {
          return api;
        },
        connect: function () {
          return api;
        }
      };
      return api;
    };

    try {
      // 定义成不可写：页面后面若还引了 CDN 上的 socket.io，也覆盖不掉这个替身
      Object.defineProperty(window, 'io', {
        configurable: false,
        enumerable: true,
        get: function () {
          return makeStub;
        },
        set: function () {
          /* 故意忽略 */
        }
      });
    } catch (err) {
      window.io = makeStub;
    }
  }

  /* ---------------------------------------- 2. 被剥掉的外部库的替身 */
  if (CFG.externalAssets === false) {
    window.hljs = window.hljs || {
      highlightAll: function () {},
      highlightElement: function () {},
      configure: function () {},
      getLanguage: function () {
        return false;
      },
      registerLanguage: function () {}
    };
  }

  /* ------------------------------------------------ 3. 路径补前缀 */
  function fix(url) {
    if (typeof url !== 'string' || url.length === 0) return url;
    if (url.charAt(0) !== '/') return url; // 相对路径 / 绝对 URL / 锚点：不动
    if (url.charAt(1) === '/') return url; // 协议相对 //cdn/x：不动
    if (url === MOUNT || url.indexOf(MOUNT + '/') === 0) return url; // 已经有了
    return MOUNT + url;
  }

  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      try {
        if (typeof input === 'string') {
          input = fix(input);
        } else if (window.Request && input instanceof Request) {
          var parsed = new URL(input.url, location.href);
          var rebuilt = fix(parsed.pathname) + parsed.search;
          if (rebuilt !== parsed.pathname + parsed.search) {
            input = new Request(rebuilt, input);
          }
        }
      } catch (err) {
        /* 改写失败就原样发出去，让请求自己失败，别把异常藏起来 */
      }
      return nativeFetch.call(this, input, init);
    };
  }

  var nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function () {
    var args = Array.prototype.slice.call(arguments);
    if (typeof args[1] === 'string') args[1] = fix(args[1]);
    return nativeOpen.apply(this, args);
  };

  try {
    ['assign', 'replace'].forEach(function (name) {
      var original = window.location[name];
      if (typeof original !== 'function') return;
      window.location[name] = function (url) {
        return original.call(window.location, fix(url));
      };
    });
  } catch (err) {
    /* location 的成员在某些浏览器上是只读的，改不动就算了 —— 
       静态改写已经覆盖了写死路径那种情况 */
  }

  /* ------------------------------------- 4. 前端路由的 history 跳转 */
  /* BrowserRouter 这类前端路由会调 history.pushState 改地址栏。不拦的话，
     模块内部一点导航，iframe 的地址就变成 /admin —— 脱离了 /portal 前缀。
     后果是用户一刷新就 404：中台根本没有 /admin 这个挂载点。
     注意这只保证「URL 不跑出前缀」，组件树里 basename 那件事另说（见 MODULES.md）。 */
  try {
    ['pushState', 'replaceState'].forEach(function (name) {
      var original = window.history[name];
      if (typeof original !== 'function') return;
      window.history[name] = function (state, title, url) {
        if (arguments.length >= 3) {
          arguments[2] = fix(url);
        }
        return original.apply(this, arguments);
      };
    });
  } catch (err) {
    /* 同上，改不动就交给静态改写兜底 */
  }
})();
