/**
 * 前端「打开卡片」链路的源码守卫（纯 Node，不需要浏览器）。
 *
 * 为什么要有它
 * ------------
 * 卡片磁贴早先用 `window.open(url, '_blank', 'noopener,noreferrer')` 打开新标签页，
 * 并拿它的返回值判断"是否被浏览器拦截"。但按 HTML 规范，
 * **只要 windowFeatures 里带 `noopener`，window.open() 就固定返回 null**，
 * 哪怕新标签页已经正常打开（`noreferrer` 同样隐含 noopener）。
 *
 * 结果：每次点击都命中 `Boolean(win) === false`，弹出
 * 「浏览器拦截了新标签页，请允许本站弹出窗口后重试」——而用户其实每次都打开了页面。
 * 这个坑肉眼很难发现：提示出现了，页面也确实开了，只是提示在说谎。
 *
 * 修法是改用真实 `<a href target="_blank" rel="noopener noreferrer">`：
 * 链接导航属于用户主动导航，不受弹窗拦截策略约束，顺带还支持
 * 中键 / Ctrl+点击 / 右键「在新标签页打开」。
 *
 * 这个脚本把"别再退回去"变成可执行的断言：
 * 扫描前端源码里真正生效的代码（注释会被剥掉，所以本文件里的说明文字不会误伤），
 * 一旦有人重新引入 window.open 式的打开逻辑就立刻失败。
 *
 * 运行：
 *     node scripts/check_launcher.mjs
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, sep } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
// 默认扫仓库里的前端源码；DTB_FRONTEND_SRC 可指向别处，
// 用来验证「守卫本身真的会失败」（把 bug 注回去跑一遍）。
const SRC = process.env.DTB_FRONTEND_SRC || join(HERE, '..', '..', 'frontend', 'src');

const PASSED = [];
const FAILED = [];

function check(label, ok, detail = '') {
  if (ok) {
    PASSED.push(label);
    console.log(`  PASS  ${label}`);
  } else {
    FAILED.push(`${label}${detail ? ' —— ' + detail : ''}`);
    console.log(`  FAIL  ${label}${detail ? ' —— ' + detail : ''}`);
  }
}

// ---------------------------------------------------------------- 工具函数

/**
 * 剥掉注释，但保留字符串字面量与行号（用空行占位）。
 *
 * 必要性：本套代码里刻意用大段注释记录了 window.open 的踩坑经过，
 * 那些说明文字里必然出现 `window.open` / 「浏览器拦截」等字样。
 * 不剥注释的话，守卫会被自己的文档绊倒。
 *
 * 做法是逐字符扫描：遇到引号进入字符串态（字符串里的 `//`、`/*` 不算注释），
 * 行注释整行丢弃，块注释换成等量的空行。
 */
function stripComments(src) {
  let out = '';
  let quote = null;
  let i = 0;
  const n = src.length;

  while (i < n) {
    const c = src[i];
    const d = src[i + 1];

    if (quote) {
      out += c;
      if (c === '\\') {
        out += d ?? '';
        i += 2;
        continue;
      }
      if (c === quote) quote = null;
      i += 1;
      continue;
    }

    if (c === '/' && d === '/') {
      while (i < n && src[i] !== '\n') i += 1;
      continue; // 保留换行本身，行号不漂移
    }

    if (c === '/' && d === '*') {
      i += 2;
      while (i < n && !(src[i] === '*' && src[i + 1] === '/')) {
        out += src[i] === '\n' ? '\n' : '';
        i += 1;
      }
      i += 2;
      continue;
    }

    if (c === "'" || c === '"' || c === '`') quote = c;
    out += c;
    i += 1;
  }

  return out;
}

/** 递归收集 .ts / .tsx */
function walk(dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) found.push(...walk(full));
    else if (/\.tsx?$/.test(entry)) found.push(full);
  }
  return found;
}

/** 把源码里所有"生效代码"（已剥注释）按文件读进来 */
const files = walk(SRC).map((path) => ({
  rel: relative(SRC, path).split(sep).join('/'),
  raw: readFileSync(path, 'utf8'),
  code: stripComments(readFileSync(path, 'utf8')),
}));

function find(rel) {
  const hit = files.find((f) => f.rel === rel);
  if (!hit) throw new Error(`找不到源文件 ${rel}，脚本需要更新`);
  return hit;
}

/** 压缩空白，避免断言被排版差异绊倒 */
const squash = (s) => s.replace(/\s+/g, ' ');

// ---------------------------------------------------------------- 断言

console.log('\n[1] 不得再用 window.open 打开卡片（返回值不可靠，且踩 noopener 规范坑）');
{
  const offenders = [];
  for (const f of files) {
    const re = /window\s*\.\s*open\s*\(/g;
    let m;
    while ((m = re.exec(f.code)) !== null) {
      const line = f.code.slice(0, m.index).split('\n').length;
      offenders.push(`${f.rel}:${line}`);
    }
  }
  check(
    '源码里没有 window.open 调用',
    offenders.length === 0,
    offenders.length ? `发现 ${offenders.join(', ')}` : '',
  );
}

console.log('\n[2] 不得再出现基于「被拦截」的假提示');
{
  const offenders = files
    .filter((f) => f.code.includes('浏览器拦截'))
    .map((f) => f.rel);
  check(
    '源码里没有「浏览器拦截」文案',
    offenders.length === 0,
    offenders.length ? `仍在 ${offenders.join(', ')}` : '',
  );
}

console.log('\n[3] 卡片磁贴必须是真实的 <a target="_blank">');
{
  const tile = find('components/CardTile.tsx');
  const code = squash(tile.code);

  check('磁贴用的是 <a 标签而不是 <button>', /<a\s/.test(tile.code));
  check('保留 .tile-open 类名（样式依赖它）', code.includes('className="tile-open"'));
  // href 必须是"算出来的"入口地址之一：
  //   没有环境地址 → buildLaunchTarget 给出的 url
  //   挂了环境地址 → 首条环境自己的 launch_url（这样中键/Ctrl+点击至少不会变成空链接）
  // 两者都不允许退化成手写路径或直接拼 target_url。
  const hrefOk =
    code.includes('href={url}') ||
    /href=\{hasEndpoints \? card\.endpoints\[0\]\.(launch_url|url)/.test(code);
  check('href 绑定的是算出来的入口地址', hrefOk);
  check('磁贴里没有手写 /gw/ 路径', !code.includes('/gw/'));
  check(
    '新标签页由 target="_blank" 承担',
    code.includes("target={newTab ? '_blank' : undefined}"),
  );
  check(
    '新标签页带上 rel="noopener noreferrer"',
    code.includes("rel={newTab ? 'noopener noreferrer' : undefined}"),
  );
}

console.log('\n[4] 打开地址的计算仍走 launcher 纯函数');
{
  const launcher = find('lib/launcher.ts');
  const code = squash(launcher.code);

  check('导出 buildLaunchTarget', code.includes('export function buildLaunchTarget'));
  check('导出 reportClick（点击计数未丢）', code.includes('export function reportClick'));
  check(
    'launch_url 为空时回退 target_url（老数据 / 导入配置兼容）',
    code.includes('card.launch_url || card.target_url'),
  );
  check(
    '不再导出会误报的 openCard / previewCard',
    !code.includes('function openCard') && !code.includes('function previewCard'),
  );
}

console.log('\n[5] 后台「预览」也不再依赖 window.open');
{
  const admin = find('pages/AdminPage.tsx');
  const code = squash(admin.code);

  check(
    '预览改用 <a href=... target="_blank">',
    /<a [^>]*className="btn btn-sm btn-ghost"[^>]*href=\{buildLaunchTarget\(card\)\.url\}/.test(
      code,
    ) || code.includes('href={buildLaunchTarget(card).url}'),
  );
  check(
    '预览链接带 rel="noopener noreferrer"',
    code.includes('rel="noopener noreferrer"'),
  );
}

console.log('\n[6] 首页点击回调只做计数与提示，不做跳转');
{
  const page = find('pages/ToolboxPage.tsx');
  const code = squash(page.code);

  check('引入的是 reportClick 而不是打开函数', code.includes('import { reportClick }'));
  check(
    '代理模式仍给出「正在通过跳板机打开」提示',
    code.includes('正在通过跳板机打开'),
  );
  check(
    '回调收到了事件对象（排序模式下要能阻止跳转）',
    code.includes('event: ReactMouseEvent<HTMLAnchorElement>'),
  );
  check(
    '排序模式下先 preventDefault 再返回',
    code.includes('if (reorderMode) { event.preventDefault() return }'),
  );
  // 挂了环境地址的卡片：点击必须先拦下再弹列表，绝不能直接放行跳转
  check(
    '多环境卡片点击先 preventDefault 再弹列表',
    code.includes('if (card.endpoints.length > 0) { event.preventDefault() setPicking(card) return }'),
  );
  check(
    '环境列表里选中的那条才上报点击',
    code.includes('reportClick(card, endpoint.slug)'),
  );
}

console.log('\n' + '='.repeat(62));
console.log(`通过 ${PASSED.length} 项，失败 ${FAILED.length} 项`);
if (FAILED.length) {
  console.log('\n失败明细：');
  for (const item of FAILED) console.log('  -', item);
  console.log(
    '\n提示：卡片打开一律用 <a href target="_blank" rel="noopener noreferrer">，' +
      '不要用 window.open 的返回值判断是否被拦截。',
  );
}
console.log('='.repeat(62));
process.exit(FAILED.length ? 1 : 0);
