/**
 * OpsGen - Core frontend utilities
 */
const OpsGen = {
  copyText(text) {
    navigator.clipboard.writeText(text).then(() => {
      this._toast('已复制到剪贴板');
    }).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      this._toast('已复制到剪贴板');
    });
  },

  copyCode(elementId) {
    const el = document.getElementById(elementId);
    if (el) {
      this.copyText(el.textContent);
    }
  },

  highlightAll() {
    document.querySelectorAll('pre code').forEach((block) => {
      const panel = block.closest('[data-panel]');
      const filename = panel?.dataset.panel || '';
      if (filename.endsWith('.yml') || filename.endsWith('.yaml')) {
        block.className = 'language-yaml';
      } else if (filename === 'Dockerfile') {
        block.className = 'language-dockerfile';
      } else {
        block.className = 'language-bash';
      }
      hljs.highlightElement(block);
    });
  },

  initTabs() {
    const tabs = document.querySelectorAll('.tab-btn');
    const panels = document.querySelectorAll('.code-panel');

    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.tab;
        tabs.forEach((t) => t.classList.remove('active'));
        panels.forEach((p) => p.classList.remove('active'));
        tab.classList.add('active');
        document.querySelector(`[data-panel="${target}"]`)?.classList.add('active');
      });
    });
  },

  _toast(message) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    toast.style.cssText = `
      position: fixed; bottom: 2rem; left: 50%; transform: translateX(-50%);
      background: rgba(0,0,0,0.8); color: #fff; padding: 0.75rem 1.5rem;
      border-radius: 10px; z-index: 9999; font-size: 0.9rem;
      animation: fadeInUp 0.3s ease;
    `;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2500);
  },
};

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.template-card').forEach((card, i) => {
    card.style.animationDelay = `${i * 0.08}s`;
    card.style.animation = 'fadeInUp 0.5s ease forwards';
    card.style.opacity = '0';
  });
});

const style = document.createElement('style');
style.textContent = `
  @keyframes fadeInUp {
    from { opacity: 0; transform: translateY(20px); }
    to { opacity: 1; transform: translateY(0); }
  }
`;
document.head.appendChild(style);
