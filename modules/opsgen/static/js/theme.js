/**
 * OpsGen - Light / Dark theme toggle
 */
(function () {
  const STORAGE_KEY = 'opsgen-theme';
  const HLJS_DARK = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css';
  const HLJS_LIGHT = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css';

  function getTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(STORAGE_KEY, theme);
    const hljsLink = document.getElementById('hljs-theme');
    if (hljsLink) {
      hljsLink.href = theme === 'light' ? HLJS_LIGHT : HLJS_DARK;
    }
    const btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.textContent = theme === 'light' ? '🌙' : '☀️';
      btn.title = theme === 'light' ? '切换到暗色' : '切换到亮色';
    }
    document.querySelectorAll('pre code.hljs').forEach((block) => {
      block.removeAttribute('data-highlighted');
      block.classList.remove('hljs');
      if (window.hljs) hljs.highlightElement(block);
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(getTheme());
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      applyTheme(getTheme() === 'dark' ? 'light' : 'dark');
    });
  });

  window.OpsGenTheme = { applyTheme, getTheme };
})();
