/**
 * OpsGen - Template list: search, favorites
 */
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('template-search-form');
  const searchInput = document.getElementById('search-input');
  const categoryFilter = document.getElementById('category-filter');
  const sourceFilter = document.getElementById('source-filter');
  const sortFilter = document.getElementById('sort-filter');
  const perPage = document.getElementById('per-page');
  const favCheck = form?.querySelector('input[name="fav"]');

  if (!form) return;

  let debounceTimer = null;

  function submitForm() {
    form.requestSubmit();
  }

  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(submitForm, 400);
    });
  }

  [categoryFilter, sourceFilter, sortFilter, perPage, favCheck].forEach((el) => {
    if (el) el.addEventListener('change', submitForm);
  });

  document.querySelectorAll('.favorite-btn').forEach((btn) => {
    btn.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const name = btn.dataset.name;
      try {
        const response = await fetch(`/api/favorites/${name}`, { method: 'POST' });
        const data = await response.json();
        if (response.ok) {
          btn.classList.toggle('active', data.favorite);
          btn.textContent = data.favorite ? '★' : '☆';
          btn.title = data.favorite ? '取消收藏' : '收藏';
          OpsGen._toast(data.favorite ? '已收藏' : '已取消收藏');
        }
      } catch {
        OpsGen._toast('操作失败');
      }
    });
  });
});
