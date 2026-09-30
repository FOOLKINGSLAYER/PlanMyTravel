(() => {
  const menu = document.querySelector('.menu-toggle');
  const nav = document.querySelector('#primary-nav');
  if (menu && nav) {
    menu.addEventListener('click', () => {
      const expanded = menu.getAttribute('aria-expanded') === 'true';
      menu.setAttribute('aria-expanded', String(!expanded));
      nav.classList.toggle('is-open', !expanded);
      document.body.classList.toggle('nav-open', !expanded);
    });
    nav.addEventListener('click', event => {
      if (event.target.closest('a')) {
        menu.setAttribute('aria-expanded', 'false');
        nav.classList.remove('is-open');
        document.body.classList.remove('nav-open');
      }
    });
  }
  document.querySelectorAll('[data-save-toggle]').forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      const saved = button.classList.toggle('is-saved');
      button.setAttribute('aria-pressed', String(saved));
      if (button.classList.contains('save-button')) button.textContent = saved ? '♥' : '♡';
      else button.textContent = saved ? '♥ Saved' : '♡ Save trip';
    });
  });
  const adminMenu = document.querySelector('.admin-menu-toggle');
  const sidebar = document.querySelector('.admin-sidebar');
  if (adminMenu && sidebar) {
    adminMenu.addEventListener('click', () => {
      const expanded = adminMenu.getAttribute('aria-expanded') === 'true';
      adminMenu.setAttribute('aria-expanded', String(!expanded));
      sidebar.classList.toggle('is-open', !expanded);
    });
    document.addEventListener('click', event => {
      if (window.innerWidth < 900 && !sidebar.contains(event.target) && !adminMenu.contains(event.target)) {
        sidebar.classList.remove('is-open');
        adminMenu.setAttribute('aria-expanded', 'false');
      }
    });
  }
})();
