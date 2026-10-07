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

  document.querySelectorAll('input[type="file"][multiple]').forEach(input => {
    const status = input.closest('form')?.querySelector('[data-image-selection-status]');
    if (!status) return;
    input.addEventListener('change', () => {
      const count = input.files?.length || 0;
      status.textContent = count
        ? `${count} image${count === 1 ? '' : 's'} selected`
        : '';
    });
  });

  document.addEventListener('submit', event => {
    const form = event.target.closest('form');
    const submitter = event.submitter;
    const message = submitter?.dataset.confirm || form?.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
  document.querySelectorAll('[data-save-toggle]').forEach(button => {
    button.addEventListener('click', async event => {
      event.preventDefault();
      event.stopPropagation();
      const itemType = button.dataset.itemType;
      const itemId = button.dataset.itemId;
      if (!itemType || !itemId) {
        window.alert('This item cannot be saved yet.');
        return;
      }
      button.disabled = true;
      try {
        const isSaved = button.getAttribute('aria-pressed') === 'true';
        const response = await fetch('/api/favorites', {
          method: isSaved ? 'DELETE' : 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrfToken || '',
          },
          body: JSON.stringify({ item_type: itemType, item_id: itemId }),
        });

        const result = await response.json();
        if (!response.ok) {
          if (response.status === 401) {
            const next = `${window.location.pathname}${window.location.search}`;
            window.location.assign(`/login?next=${encodeURIComponent(next)}`);
            return;
          }
          throw new Error(result.error?.message || 'Could not update your saved items.');
        }
        button.setAttribute('aria-pressed', String(result.saved));
        button.classList.toggle('is-saved', result.saved);
        if (button.classList.contains('save-button')) {
          button.textContent = result.saved ? '♥' : '♡';
        } else {
          button.textContent = result.saved ? '♥ Saved' : '♡ Save destination';
        }
      } catch (error) {
        window.alert(error instanceof Error ? error.message : 'Could not update your saved items.');
      } finally {
        button.disabled = false;
      }
    });
  });

  const searchForm = document.querySelector('[data-live-search]');
  const searchResults = document.querySelector('#search-results');
  if (searchForm && searchResults) {
    let timer;
    let activeRequest;
    const updateResults = async (targetUrl, addHistory = true) => {
      activeRequest?.abort();
      activeRequest = new AbortController();
      const url = new URL(targetUrl, window.location.origin);
      url.searchParams.set('partial', '1');
      searchResults.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(url, {
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          signal: activeRequest.signal,
        });
        if (!response.ok) throw new Error('Search could not be updated.');
        searchResults.innerHTML = await response.text();
        const visibleUrl = new URL(targetUrl, window.location.origin);
        visibleUrl.searchParams.delete('partial');
        if (addHistory) history.pushState({}, '', visibleUrl);
      } catch (error) {
        if (error.name !== 'AbortError') {
          searchResults.insertAdjacentHTML('afterbegin', '<p class="alert alert-error" role="alert">Search could not be updated. Please try again.</p>');
        }
      } finally {
        searchResults.setAttribute('aria-busy', 'false');
      }
    };
    const submitSearch = (addHistory = true) => {
      const query = new URLSearchParams(new FormData(searchForm));
      query.delete('page');
      updateResults(`${searchForm.action}?${query}`, addHistory);
    };
    searchForm.addEventListener('submit', event => {
      event.preventDefault();
      submitSearch();
    });
    searchForm.addEventListener('input', event => {
      if (event.target.name === 'q' || event.target.name === 'budget_min' || event.target.name === 'budget_max' || event.target.name === 'category' || event.target.name === 'style') {
        clearTimeout(timer);
        timer = setTimeout(() => submitSearch(), event.target.name === 'q' ? 250 : 350);
      }
    });
    searchForm.addEventListener('change', () => submitSearch());
    searchResults.addEventListener('click', event => {
      const link = event.target.closest('.search-pagination a');
      if (!link) return;
      event.preventDefault();
      updateResults(link.href);
    });
    window.addEventListener('popstate', () => {
      updateResults(window.location.href, false);
    });
  }

  document.querySelectorAll('[data-gallery]').forEach(gallery => {
    const images = [...gallery.querySelectorAll('[data-gallery-image]')];
    if (!images.length) return;
    const thumbnails = [...gallery.querySelectorAll('[data-gallery-thumb]')];
    const counter = gallery.querySelector('[data-gallery-counter]');
    const dialog = document.querySelector('[data-gallery-dialog]');
    const lightboxImage = dialog?.querySelector('[data-lightbox-image]');
    const lightboxCounter = dialog?.querySelector('[data-lightbox-counter]');
    const mosaic = gallery.hasAttribute('data-gallery-mosaic');
    let currentIndex = 0;

    const showImage = index => {
      currentIndex = (index + images.length) % images.length;
      images.forEach((image, imageIndex) => {
        if (!mosaic) image.hidden = imageIndex !== currentIndex;
        image.classList.toggle('is-active', imageIndex === currentIndex);
      });
      thumbnails.forEach((thumbnail, imageIndex) => {
        const active = imageIndex === currentIndex;
        thumbnail.classList.toggle('is-active', active);
        thumbnail.setAttribute('aria-pressed', String(active));
      });
      const label = `${currentIndex + 1} / ${images.length}`;
      if (counter) counter.textContent = label;
      if (lightboxCounter) lightboxCounter.textContent = label;
      if (lightboxImage) {
        lightboxImage.src = images[currentIndex].src;
        lightboxImage.alt = images[currentIndex].alt;
      }
    };

    gallery.addEventListener('click', event => {
      const thumbnail = event.target.closest('[data-gallery-thumb]');
      if (thumbnail) showImage(Number(thumbnail.dataset.galleryThumb));
      if (event.target.closest('[data-gallery-prev]')) showImage(currentIndex - 1);
      if (event.target.closest('[data-gallery-next]')) showImage(currentIndex + 1);
      if (event.target.closest('[data-gallery-open]') && dialog?.showModal) dialog.showModal();
    });
    dialog?.addEventListener('click', event => {
      if (event.target.closest('[data-gallery-close]')) dialog.close();
      if (event.target.closest('[data-gallery-prev]')) showImage(currentIndex - 1);
      if (event.target.closest('[data-gallery-next]')) showImage(currentIndex + 1);
    });
    dialog?.addEventListener('keydown', event => {
      if (event.key === 'ArrowLeft') showImage(currentIndex - 1);
      if (event.key === 'ArrowRight') showImage(currentIndex + 1);
    });
    let touchStartX;
    gallery.addEventListener('touchstart', event => {
      touchStartX = event.changedTouches[0].screenX;
    }, { passive: true });
    gallery.addEventListener('touchend', event => {
      if (touchStartX === undefined) return;
      const difference = event.changedTouches[0].screenX - touchStartX;
      if (Math.abs(difference) > 40) showImage(currentIndex + (difference < 0 ? 1 : -1));
      touchStartX = undefined;
    }, { passive: true });
    gallery.querySelectorAll('img').forEach(image => {
      image.addEventListener('error', () => {
        image.src = '/images/placeholders/default.jpg';
      }, { once: true });
    });
    showImage(0);
  });

  document.querySelectorAll('[data-share-package]').forEach(button => {
    button.addEventListener('click', async () => {
      const shareData = { title: document.title, url: window.location.href };
      try {
        if (navigator.share) {
          await navigator.share(shareData);
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(shareData.url);
          button.textContent = '✓ Link copied';
        } else {
          window.prompt('Copy this package link:', shareData.url);
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          window.alert('This package link could not be shared.');
        }
      }
    });
  });

  document.querySelectorAll('[data-share-trip]').forEach(button => {
    button.addEventListener('click', async () => {
      const shareData = { title: document.title, url: window.location.href };
      try {
        if (navigator.share) {
          await navigator.share(shareData);
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(shareData.url);
          button.textContent = '✓ Link copied';
        } else {
          window.prompt('Copy this trip link:', shareData.url);
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          window.alert('This trip link could not be shared.');
        }
      }
    });
  });

  document.querySelectorAll('[data-print-page]').forEach(button => {
    button.addEventListener('click', () => window.print());
  });

  document.querySelectorAll('.planner-tabs a').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = document.querySelector(tab.getAttribute('href'));
      target?.closest('details')?.setAttribute('open', '');
      document.querySelectorAll('.planner-tabs a').forEach(item => {
        item.classList.toggle('is-active', item === tab);
        if (item === tab) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
      });
    });
  });

  document.querySelectorAll('[data-trip-prompt]').forEach(button => {
    button.addEventListener('click', () => {
      const prompt = document.querySelector('.planner-prompt input[name="notes"]');
      if (!prompt) return;
      prompt.value = button.dataset.tripPrompt || '';
      prompt.focus();
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

  document.querySelectorAll('.category-row').forEach(row => {
    const next = row.querySelector('.category-nav');
    next?.addEventListener('click', () => {
      row.scrollBy({ left: row.clientWidth * 0.75, behavior: 'smooth' });
    });
  });
})();
