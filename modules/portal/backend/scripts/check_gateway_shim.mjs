/**
 * 网关客户端 shim 的行为验证（纯 Node，不需要浏览器）。
 *
 * 为什么要有它
 * ------------
 * shim 是一段会被注入到内网页面里的 JS。它一旦语法出错或逻辑写反，
 * 被代理的页面就会整个白屏 —— 而这类问题用 Python 断言是测不出来的
 * （字节级重写只能证明"脚本被注入了"，不能证明"脚本能跑对"）。
 *
 * 这里搭一个最小 DOM 沙箱把 shim 跑起来，验证它确实把
 * `fetch('/api/x')` 改写成 `fetch('/gw/<slug>/api/x')`，且不误伤相对路径与外链。
 *
 * 运行：
 *     node scripts/check_gateway_shim.mjs
 *
 * shim 源码直接从 `app/services/proxy_service.py` 里抽取，保证测的就是真正上线的那份。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createContext, runInContext } from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = join(HERE, '..', 'app', 'services', 'proxy_service.py');

const PREFIX = '/gw/demo/';
// 卡片目标地址对应的 origin / 基准目录，shim 用它识别"与目标同源的绝对地址"。
// 真实案例：Jenkins 页面里 `<link href="http://10.0.0.20:8081/theme-dark/theme.css">`
// 是绝对地址，跳板机场景下浏览器访问不到该 host，必须收进网关前缀。
const ORIGIN = 'http://127.0.0.1:9099';
const BASE_PATH = '/';

// ---------------------------------------------------------------- 抽取 shim
const py = readFileSync(SOURCE, 'utf8');
const start = py.indexOf('_CLIENT_SHIM_JS = r"""');
if (start < 0) {
  console.error('找不到 _CLIENT_SHIM_JS，请确认 python 文件结构未变');
  process.exit(1);
}
const bodyStart = start + '_CLIENT_SHIM_JS = r"""'.length;
const bodyEnd = py.indexOf('"""', bodyStart);
if (bodyEnd < 0) {
  console.error('_CLIENT_SHIM_JS 的三引号没有闭合');
  process.exit(1);
}
const SHIM_TEMPLATE = py.slice(bodyStart, bodyEnd);

function renderShim({ prefix, origin, basePath }) {
  return SHIM_TEMPLATE.replace('__PREFIX__', JSON.stringify(prefix))
    .replace('__ORIGIN__', JSON.stringify(origin))
    .replace('__BASEPATH__', JSON.stringify(basePath));
}
const SHIM = renderShim({ prefix: PREFIX, origin: ORIGIN, basePath: BASE_PATH });

const PASSED = [];
const FAILED = [];

function check(name, ok, detail = '') {
  (ok ? PASSED : FAILED).push(ok ? name : `${name} :: ${detail}`);
  console.log(ok ? `  \x1b[32mPASS\x1b[0m  ${name}` : `  \x1b[31mFAIL\x1b[0m  ${name}  ${detail}`);
}

// ------------------------------------------------------------ 最小 DOM 沙箱
function makeAccessorProto(prop) {
  const proto = {};
  Object.defineProperty(proto, prop, {
    configurable: true,
    enumerable: true,
    get() {
      return this[`_${prop}`];
    },
    set(v) {
      this[`_${prop}`] = v;
    },
  });
  return proto;
}

function buildSandbox() {
  const Element = {
    prototype: {
      setAttribute(name, value) {
        this.attrs = this.attrs || {};
        this.attrs[name] = value;
      },
    },
  };

  function XMLHttpRequest() {}
  XMLHttpRequest.prototype.open = function (method, url) {
    this._url = url;
  };

  class FakeWebSocket {
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
    }
  }
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;

  class FakeEventSource {
    constructor(url, config) {
      this.url = url;
      this.config = config;
    }
  }

  const calls = { fetch: [], beacon: [], open: [], worker: [], sw: [] };

  // 真实浏览器里 Request 一定存在；shim 在构造失败时会安全地退回原对象，
  // 但那条"降级路径"不能代替正常路径 —— 这里提供真实实现来走正常分支。
  class FakeRequest {
    constructor(url, init) {
      this.url = url;
      this.init = init;
    }
  }

  const window = {
    fetch: (input) => {
      calls.fetch.push(typeof input === 'string' ? input : input?.url);
      return Promise.resolve({ ok: true });
    },
    WebSocket: FakeWebSocket,
    EventSource: FakeEventSource,
    Worker: class Worker {
      constructor(url, options) {
        calls.worker.push(url);
        this.url = url;
        this.options = options;
      }
    },
    open: (url) => {
      calls.open.push(url);
      return null;
    },
  };

  const sandbox = {
    window,
    Element,
    XMLHttpRequest,
    HTMLScriptElement: { prototype: makeAccessorProto('src') },
    HTMLImageElement: { prototype: makeAccessorProto('src') },
    HTMLIFrameElement: { prototype: makeAccessorProto('src') },
    HTMLMediaElement: { prototype: makeAccessorProto('src') },
    HTMLSourceElement: { prototype: makeAccessorProto('src') },
    HTMLLinkElement: { prototype: makeAccessorProto('href') },
    HTMLAnchorElement: { prototype: makeAccessorProto('href') },
    HTMLAreaElement: { prototype: makeAccessorProto('href') },
    HTMLFormElement: { prototype: makeAccessorProto('action') },
    HTMLObjectElement: { prototype: makeAccessorProto('data') },
    navigator: {
      sendBeacon: (url, data) => {
        calls.beacon.push(url);
        return true;
      },
      serviceWorker: {
        register: (url, options) => {
          calls.sw.push(url);
          return Promise.resolve({});
        },
      },
    },
    console,
    Promise,
    Request: FakeRequest,
  };
  return { sandbox, calls, window, XMLHttpRequest, Element };
}

// ------------------------------------------------------------------- 执行
console.log('\n[1] 加载 shim');
const env = buildSandbox();
try {
  runInContext(SHIM, createContext(env.sandbox), { filename: 'gateway-shim.js' });
  check('shim 语法正确且加载不抛错', true);
} catch (err) {
  check('shim 语法正确且加载不抛错', false, String(err));
  console.log(`\n通过 ${PASSED.length} 项，失败 ${FAILED.length} 项`);
  process.exit(1);
}
check('挂上了 __dtbGatewayPrefix', env.window.__dtbGatewayPrefix === PREFIX, String(env.window.__dtbGatewayPrefix));
check(
  '模板占位符已全部替换（没有残留 __XXX__）',
  !/__[A-Z]+__/.test(SHIM),
  (SHIM.match(/__[A-Z]+__/g) || []).join(','),
);

console.log('\n[2] fetch URL 改写');
const fetchUrl = async (url) => {
  env.calls.fetch.length = 0;
  await env.window.fetch(url);
  return env.calls.fetch[0];
};

const cases = [
  ['/api/v1/game-config', `${PREFIX}api/v1/game-config`, '根路径接口被加上前缀'],
  ['/', PREFIX, '根路径单独被替换成前缀'],
  ['a/b.js', 'a/b.js', '相对路径交给 <base>，不动'],
  ['./x.css', './x.css', '相对路径不动'],
  ['//cdn.example.com/lib.js', '//cdn.example.com/lib.js', '协议相对外链不动'],
  ['https://example.com/x', 'https://example.com/x', '绝对外链不动'],
  ['/gw/demo/already', '/gw/demo/already', '已带前缀时保持幂等'],
  ['data:text/plain,x', 'data:text/plain,x', 'data: 不动'],
  ['', '', '空串原样返回'],
  // —— 与目标同源的绝对地址：必须收进网关（否则客户端直连目标 host 会失败）——
  [
    `${ORIGIN}/theme-dark/theme.css`,
    `${PREFIX}theme-dark/theme.css`,
    '同源绝对地址被改写',
  ],
  [`${ORIGIN}/`, PREFIX, '同源绝对地址的根路径'],
  [`${ORIGIN}/a/b?q=1#f`, `${PREFIX}a/b?q=1#f`, '同源绝对地址保留查询串与锚点'],
  [
    `//127.0.0.1:9099/a.png`,
    `${PREFIX}a.png`,
    '同源协议相对地址被改写',
  ],
  [
    `https://127.0.0.1:9099/x.css`,
    `https://127.0.0.1:9099/x.css`,
    '同源但协议不同，不改写',
  ],
  [
    `http://127.0.0.1:9098/x.css`,
    `http://127.0.0.1:9098/x.css`,
    '同主机但端口不同，不改写',
  ],
  [`http://example.com/x.css`, `http://example.com/x.css`, '同名路径但不同主机，不改写'],
];

for (const [input, expected, label] of cases) {
  const actual = await fetchUrl(input);
  check(label, actual === expected, `期望 ${expected}，实际 ${actual}`);
}

console.log('\n[3] 其他入口');
env.calls.fetch.length = 0;
await env.window.fetch({ url: '/api/v2/state' });
check('fetch(Request 对象) 也会改写', env.calls.fetch[0] === `${PREFIX}api/v2/state`, String(env.calls.fetch[0]));

const xhr = new env.XMLHttpRequest();
xhr.open('GET', '/api/x');
check('XMLHttpRequest.open 被改写', xhr._url === `${PREFIX}api/x`, String(xhr._url));

const ws = new env.window.WebSocket('/ws/notify');
check('WebSocket 地址被改写', ws.url === `${PREFIX}ws/notify`, String(ws.url));
check('WebSocket 常量保留', env.window.WebSocket.OPEN === 1, String(env.window.WebSocket.OPEN));
check('WebSocket instanceof 仍然成立', ws instanceof env.window.WebSocket, 'prototype 未保持');

const es = new env.window.EventSource('/sse/stream');
check('EventSource 地址被改写', es.url === `${PREFIX}sse/stream`, String(es.url));

new env.window.Worker('/worker.js');
check('Worker 地址被改写', env.calls.worker[0] === `${PREFIX}worker.js`, String(env.calls.worker[0]));

env.window.open('/page2');
check('window.open 地址被改写', env.calls.open[0] === `${PREFIX}page2`, String(env.calls.open[0]));

env.sandbox.navigator.sendBeacon('/track', 'x');
check('sendBeacon 地址被改写', env.calls.beacon[0] === `${PREFIX}track`, String(env.calls.beacon[0]));

env.sandbox.navigator.serviceWorker.register('/sw.js');
check('ServiceWorker.register 地址被改写', env.calls.sw[0] === `${PREFIX}sw.js`, String(env.calls.sw[0]));

const el = Object.create(env.Element.prototype);
el.setAttribute('src', '/img/a.png');
check('setAttribute 改写 URL 属性', el.attrs.src === `${PREFIX}img/a.png`, String(el.attrs.src));
el.setAttribute('data-name', '/img/a.png');
check('setAttribute 不动非 URL 属性', el.attrs['data-name'] === '/img/a.png', String(el.attrs['data-name']));

const img = Object.create(env.sandbox.HTMLImageElement.prototype);
img.src = '/img/logo.png';
check('img.src 属性赋值被改写', img.src === `${PREFIX}img/logo.png`, String(img.src));

const a = Object.create(env.sandbox.HTMLAnchorElement.prototype);
a.href = '/page2';
check("a.href 属性赋值被改写", a.href === `${PREFIX}page2`, String(a.href));

const form = Object.create(env.sandbox.HTMLFormElement.prototype);
form.action = '/submit';
check('form.action 属性赋值被改写', form.action === `${PREFIX}submit`, String(form.action));

console.log('\n[4] 目标地址填到目录层级（基准目录非 /）');
// 卡片目标写成 http://h:8080/app/ 时，网关路径 x 对应目标 /app/x，
// 因此同源绝对地址 http://h:8080/app/y.css 应还原成 /gw/app/y.css。
const env2 = buildSandbox();
const PREFIX2 = '/gw/app/';
try {
  runInContext(
    renderShim({ prefix: PREFIX2, origin: 'http://h:8080', basePath: '/app/' }),
    createContext(env2.sandbox),
    { filename: 'gateway-shim-app.js' },
  );
  check('基准目录版 shim 加载不抛错', true);
} catch (err) {
  check('基准目录版 shim 加载不抛错', false, String(err));
}

const fetchApp = async (url) => {
  env2.calls.fetch.length = 0;
  await env2.window.fetch(url);
  return env2.calls.fetch[0];
};

const appCases = [
  // 目标写成 http://h:8080/app/ 时，网关路径本身就是相对 /app/ 的，
  // 所以根路径 /app/x.css 加前缀后得到 /gw/app/app/x.css 才是正确语义
  // （与 html 的字节级重写保持一致，两条路不能各走各的）。
  ['/app/x.css', `${PREFIX2}app/x.css`, '根路径一律加前缀'],
  ['http://h:8080/app/y.css', `${PREFIX2}y.css`, '同源绝对地址去掉基准目录再加前缀'],
  ['http://h:8080/app/', PREFIX2, '同源绝对地址指向基准目录本身'],
  ['/other/z.css', `${PREFIX2}other/z.css`, '基准目录外的根路径同样加前缀'],
  [
    'http://h:8080/other/z.css',
    'http://h:8080/other/z.css',
    '同源但落在基准目录之外的绝对地址不动',
  ],
  ['a.js', 'a.js', '相对路径不动'],
];

for (const [input, expected, label] of appCases) {
  const actual = await fetchApp(input);
  check(label, actual === expected, `期望 ${expected}，实际 ${actual}`);
}

console.log('\n' + '='.repeat(62));
console.log(`通过 ${PASSED.length} 项，失败 ${FAILED.length} 项`);
if (FAILED.length) {
  console.log('\n失败明细：');
  for (const item of FAILED) console.log('  -', item);
}
console.log('='.repeat(62));
process.exit(FAILED.length ? 1 : 0);
