(function () {
  var pollers = [];
  var buttonState = new WeakMap();
  var defaultLoadingHtml = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Loading...';

  function fetchJson(url, options) {
    var requestOptions = options || {};
    var headers = Object.assign({ Accept: 'application/json' }, requestOptions.headers || {});

    return fetch(url, Object.assign({}, requestOptions, { headers: headers })).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (payload) {
          if (!response.ok) {
            throw new Error(payload.error || payload.message || 'Request failed.');
          }
          return payload;
        });
    });
  }

  function setButtonLoading(button, isLoading, loadingHtml) {
    if (!button) {
      return;
    }

    if (isLoading) {
      if (!buttonState.has(button)) {
        buttonState.set(button, {
          html: button.innerHTML,
          value: button.value,
          width: button.offsetWidth,
        });
      }

      button.dataset.khLoading = 'true';
      button.disabled = true;
      if (button.tagName === 'BUTTON') {
        button.style.minWidth = button.offsetWidth ? button.offsetWidth + 'px' : '';
        button.innerHTML = loadingHtml || defaultLoadingHtml;
      } else {
        button.value = 'Loading...';
      }
      return;
    }

    var state = buttonState.get(button);
    if (state) {
      if (button.tagName === 'BUTTON') {
        button.innerHTML = state.html;
        button.style.minWidth = '';
      } else {
        button.value = state.value;
      }
    }

    button.disabled = false;
    delete button.dataset.khLoading;
  }

  function bindFormLoadingStates(root) {
    var scope = root || document;
    var forms = scope.querySelectorAll('form[method="post" i]');

    forms.forEach(function (form) {
      if (form.dataset.khLoadingBound === 'true') {
        return;
      }

      form.dataset.khLoadingBound = 'true';

      form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(function (submitter) {
        submitter.addEventListener('click', function () {
          form.__khSubmitter = submitter;
        });
      });

      form.addEventListener('submit', function (event) {
        if (form.dataset.skipLoading === 'true') {
          return;
        }

        if (typeof form.checkValidity === 'function' && !form.checkValidity()) {
          return;
        }

        var submitter = form.__khSubmitter || form.querySelector('button[type="submit"], input[type="submit"]');
        if (!submitter) {
          return;
        }

        if (submitter.dataset.khLoading === 'true') {
          event.preventDefault();
          return;
        }

        setButtonLoading(submitter, true, submitter.dataset.loadingHtml || defaultLoadingHtml);
      });
    });
  }

  function registerPoller(config) {
    var poller = {
      interval: config.interval,
      onTick: config.onTick,
      timer: null,
      running: false,
      active: true,
      immediate: config.immediate !== false,
    };

    function schedule() {
      clearTimeout(poller.timer);
      if (!poller.active || document.hidden) {
        return;
      }
      poller.timer = window.setTimeout(function () {
        tick().finally(schedule);
      }, poller.interval);
    }

    function tick() {
      if (poller.running || document.hidden) {
        return Promise.resolve();
      }
      poller.running = true;
      return Promise.resolve()
        .then(poller.onTick)
        .catch(function (error) {
          console.error('Polling failed:', error);
        })
        .finally(function () {
          poller.running = false;
        });
    }

    poller.start = function (forceTick) {
      poller.active = true;
      if (forceTick) {
        tick();
      }
      schedule();
    };

    poller.stop = function () {
      poller.active = false;
      clearTimeout(poller.timer);
      poller.timer = null;
    };

    pollers.push(poller);

    if (!document.hidden) {
      if (poller.immediate) {
        tick();
      }
      schedule();
    }

    return poller;
  }

  document.addEventListener('visibilitychange', function () {
    pollers.forEach(function (poller) {
      if (document.hidden) {
        clearTimeout(poller.timer);
        poller.timer = null;
        return;
      }
      if (poller.active) {
        poller.start(true);
      }
    });
  });

  window.addEventListener('pageshow', function () {
    document.querySelectorAll('[data-kh-loading="true"]').forEach(function (button) {
      setButtonLoading(button, false);
    });
  });

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function initializeUnreadBadge() {
    var messagesLink = document.querySelector('[data-unread-count-url]');
    if (!messagesLink) {
      return;
    }

    var badge = document.getElementById('navMessagesBadge');
    var url = messagesLink.dataset.unreadCountUrl;
    if (!badge || !url) {
      return;
    }

    function updateUnreadCount() {
      return fetchJson(url).then(function (payload) {
        var count = Number(payload.unread_count || 0);
        badge.textContent = String(count);
        badge.classList.toggle('d-none', count <= 0);
      });
    }

    registerPoller({ interval: 5000, onTick: updateUnreadCount });
  }

  function createChatMessageElement(message) {
    var wrapper = document.createElement('div');
    wrapper.className = 'd-flex mb-3 ' + (message.is_sender ? 'justify-content-end' : 'justify-content-start');
    wrapper.dataset.messageId = String(message.msg_id);

    var bubble = document.createElement('div');
    bubble.className = 'p-3 rounded-3 ' + (message.is_sender ? 'bg-primary text-white text-end' : 'bg-kh-soft text-kh-heading text-start');
    bubble.style.maxWidth = '75%';

    var body = document.createElement('div');
    body.textContent = message.message;

    var timestamp = document.createElement('div');
    timestamp.className = 'text-muted small mt-2';
    timestamp.textContent = message.created_at;

    bubble.appendChild(body);
    bubble.appendChild(timestamp);
    wrapper.appendChild(bubble);
    return wrapper;
  }

  function initializeChatPage() {
    var page = document.querySelector('[data-chat-page]');
    if (!page) {
      return;
    }

    var messagesBox = document.getElementById('chatMessages');
    var emptyState = document.getElementById('chatEmptyState');
    var form = document.getElementById('chatMessageForm');
    var input = document.getElementById('chatMessageInput');
    var submitButton = form ? form.querySelector('button[type="submit"]') : null;
    var pollUrl = page.dataset.pollUrl;
    var sendUrl = page.dataset.sendUrl;
    var lastMessageId = Number(page.dataset.lastMessageId || 0);
    var knownMessageIds = new Set();

    if (!messagesBox || !form || !input || !submitButton || !pollUrl || !sendUrl) {
      return;
    }

    messagesBox.querySelectorAll('[data-message-id]').forEach(function (node) {
      knownMessageIds.add(Number(node.dataset.messageId));
    });

    function scrollMessagesToBottom() {
      messagesBox.scrollTop = messagesBox.scrollHeight;
    }

    function appendMessages(messages) {
      var appended = false;
      messages.forEach(function (message) {
        var messageId = Number(message.msg_id);
        if (knownMessageIds.has(messageId)) {
          if (messageId > lastMessageId) {
            lastMessageId = messageId;
            page.dataset.lastMessageId = String(lastMessageId);
          }
          return;
        }

        knownMessageIds.add(messageId);
        lastMessageId = Math.max(lastMessageId, messageId);
        page.dataset.lastMessageId = String(lastMessageId);
        if (emptyState) {
          emptyState.remove();
          emptyState = null;
        }
        messagesBox.appendChild(createChatMessageElement(message));
        appended = true;
      });

      if (appended) {
        scrollMessagesToBottom();
      }
    }

    function pollMessages() {
      var url = new URL(pollUrl, window.location.origin);
      url.searchParams.set('after_id', String(lastMessageId));
      return fetchJson(url.toString()).then(function (payload) {
        appendMessages(payload.messages || []);
      });
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var messageText = input.value.trim();
      if (!messageText) {
        return;
      }

      setButtonLoading(submitButton, true);
      fetchJson(sendUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: new URLSearchParams({ message: messageText }).toString()
      })
        .then(function (payload) {
          if (payload.message) {
            appendMessages([payload.message]);
          }
          input.value = '';
          input.focus();
        })
        .catch(function (error) {
          console.error('Unable to send message:', error);
        })
        .finally(function () {
          setButtonLoading(submitButton, false);
        });
    });

    registerPoller({ interval: 2000, onTick: pollMessages, immediate: false });
    scrollMessagesToBottom();
  }

  function initializePropertiesPage() {
    var feed = document.querySelector('[data-properties-feed]');
    if (!feed) {
      return;
    }

    var results = document.getElementById('propertyResults');
    if (!results) {
      return;
    }

    var existingIds = new Set();
    var latestPropertyId = Number(feed.dataset.beforeId || 0);
    results.querySelectorAll('[data-property-id]').forEach(function (node) {
      var propertyId = Number(node.dataset.propertyId);
      existingIds.add(propertyId);
      latestPropertyId = Math.max(latestPropertyId, propertyId);
    });

    results.addEventListener('click', function (event) {
      var card = event.target.closest('.property-card-clickable');
      if (!card) {
        return;
      }
      if (event.target.closest('a, button, form')) {
        return;
      }
      if (card.dataset.detailUrl) {
        window.location.href = card.dataset.detailUrl;
      }
    });

    function buildPropertyCard(property) {
      return (
        '<div class="col-sm-12 col-md-6 col-lg-4" data-property-id="' + property.prop_id + '">' +
          '<div class="card h-100 border-0 property-card property-card-clickable" role="button" data-property-id="' + property.prop_id + '" data-detail-url="' + escapeHtml(property.detail_url) + '">' +
            '<img src="' + escapeHtml(property.cover_image_url) + '" class="card-img-top" alt="' + escapeHtml(property.prop_title) + '" style="height: 220px; object-fit: cover;">' +
            '<div class="card-body d-flex flex-column">' +
              '<div class="d-flex justify-content-between align-items-start mb-2 gap-2">' +
                '<h5 class="card-title mb-0">' + escapeHtml(property.prop_title) + '</h5>' +
                '<span class="badge bg-success-subtle text-success">' + escapeHtml(property.listing_type || 'Active') + '</span>' +
              '</div>' +
              '<p class="card-text text-muted mb-2">' + escapeHtml(property.prop_location || '') + (property.prop_state ? ' - ' + escapeHtml(property.prop_state) : '') + '</p>' +
              '<p class="card-text mb-2"><strong>Type:</strong> ' + escapeHtml(property.prop_type || 'Property') + '</p>' +
              '<p class="card-text mb-3">' + escapeHtml(property.short_desc || '') + '</p>' +
              '<p class="card-text fw-bold text-primary mb-3">' + escapeHtml(property.prop_price || '') + '</p>' +
              '<a href="' + escapeHtml(property.detail_url) + '" class="btn btn-add-property w-100 mt-auto">View Details</a>' +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }

    function pollProperties() {
      var url = new URL(feed.dataset.pollUrl, window.location.origin);
      url.searchParams.set('before_id', String(latestPropertyId));
      if (feed.dataset.categoryId) {
        url.searchParams.set('category_id', feed.dataset.categoryId);
      }
      if (feed.dataset.searchQuery) {
        url.searchParams.set('q', feed.dataset.searchQuery);
      }

      return fetchJson(url.toString()).then(function (payload) {
        var properties = payload.properties || [];
        if (!properties.length) {
          return;
        }

        var emptyState = results.querySelector('.empty-state-box') ? results.querySelector('.empty-state-box').closest('.col-12') : null;
        if (emptyState) {
          emptyState.remove();
        }

        properties.forEach(function (property) {
          if (existingIds.has(property.prop_id)) {
            return;
          }
          existingIds.add(property.prop_id);
          latestPropertyId = Math.max(latestPropertyId, Number(property.prop_id || 0));
          results.insertAdjacentHTML('afterbegin', buildPropertyCard(property));
        });
      });
    }

    registerPoller({ interval: 10000, onTick: pollProperties, immediate: false });
  }

  function initializeMyListingsPage() {
    var feed = document.querySelector('[data-my-listings-feed]');
    if (!feed) {
      return;
    }

    var container = document.getElementById('myListingsFeed');
    if (!container) {
      return;
    }

    var snapshot = container.dataset.snapshot || '';

    function buildListingCard(listing) {
      return (
        '<div class="col-lg-6" data-property-id="' + listing.prop_id + '">' +
          '<div class="card shadow-sm border-0 h-100">' +
            '<div class="row g-0 h-100">' +
              '<div class="col-md-5">' +
                (listing.image_url
                  ? '<img src="' + escapeHtml(listing.image_url) + '" alt="' + escapeHtml(listing.prop_title) + '" class="img-fluid rounded-start h-100 w-100" style="object-fit: cover; min-height: 220px;">'
                  : '<div class="h-100 d-flex align-items-center justify-content-center bg-kh-soft text-muted rounded-start" style="min-height: 220px;"><i class="bi bi-image fs-1"></i></div>') +
              '</div>' +
              '<div class="col-md-7">' +
                '<div class="card-body d-flex flex-column h-100">' +
                  '<div class="d-flex justify-content-between align-items-start gap-2">' +
                    '<div><h5 class="fw-bold mb-1">' + escapeHtml(listing.prop_title) + '</h5><p class="text-muted small mb-2">' + escapeHtml(listing.prop_location || '') + '</p></div>' +
                    '<span class="badge bg-success-subtle text-success">' + escapeHtml(listing.listing_type || 'Active') + '</span>' +
                  '</div>' +
                  '<p class="fw-semibold text-primary mb-3">' + escapeHtml(listing.prop_price_display || '') + '</p>' +
                  '<div class="small text-muted mb-3">' +
                    '<div class="d-flex justify-content-between py-1"><span>Status</span><strong>Active</strong></div>' +
                    '<div class="d-flex justify-content-between py-1"><span>Inquiries</span><strong>' + escapeHtml(String(listing.inquiry_count || 0)) + '</strong></div>' +
                    '<div class="d-flex justify-content-between py-1"><span>Posted</span><strong>' + escapeHtml(listing.created_at || 'Recently posted') + '</strong></div>' +
                  '</div>' +
                  '<div class="mt-auto d-flex gap-2 flex-wrap">' +
                    '<a href="' + escapeHtml(listing.view_url) + '" class="btn btn-outline-primary btn-sm"><i class="bi bi-eye me-1"></i>View</a>' +
                    '<a href="' + escapeHtml(listing.edit_url) + '" class="btn btn-outline-secondary btn-sm"><i class="bi bi-pencil-square me-1"></i>Edit</a>' +
                    '<form method="post" action="' + escapeHtml(listing.delete_url) + '" onsubmit="return confirm(\'Delete this listing permanently?\');" class="d-inline">' +
                      '<button type="submit" class="btn btn-outline-danger btn-sm"><i class="bi bi-trash me-1"></i>Delete</button>' +
                    '</form>' +
                  '</div>' +
                '</div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }

    function renderListings(listings) {
      if (!listings.length) {
        container.innerHTML =
          '<div class="card shadow-sm border-0 text-center py-5">' +
            '<div class="card-body">' +
              '<i class="bi bi-house-door display-4 text-muted mb-3"></i>' +
              '<h4 class="fw-bold mb-2">No listings yet</h4>' +
              '<p class="text-muted mb-4">You have not posted any properties. Start by sharing your first listing with potential buyers and renters.</p>' +
              '<a href="' + escapeHtml(feed.dataset.postUrl) + '" class="btn btn-primary">Post Your First Property</a>' +
            '</div>' +
          '</div>';
        bindFormLoadingStates(container);
        return;
      }

      container.innerHTML = '<div class="row g-4">' + listings.map(buildListingCard).join('') + '</div>';
      bindFormLoadingStates(container);
    }

    function pollListings() {
      return fetchJson(feed.dataset.pollUrl).then(function (payload) {
        var listings = payload.listings || [];
        var nextSnapshot = JSON.stringify(listings);
        if (nextSnapshot === snapshot) {
          return;
        }
        snapshot = nextSnapshot;
        container.dataset.snapshot = snapshot;
        renderListings(listings);
      });
    }

    registerPoller({ interval: 10000, onTick: pollListings, immediate: false });
  }

  function initializePropertyDetailPage() {
    var detail = document.querySelector('[data-property-detail]');
    if (!detail) {
      return;
    }

    var mainImage = document.getElementById('mainPropertyImage');
    var gallery = document.getElementById('propertyGallery');
    var title = document.getElementById('propertyDetailTitle');
    var price = document.getElementById('propertyDetailPrice');
    var location = document.getElementById('propertyDetailLocation');
    var description = document.getElementById('propertyDetailDescription');
    var address = document.getElementById('propertyDetailAddress');
    var favoriteButton = document.getElementById('favoriteToggle');
    var placeholderImage = detail.dataset.placeholderImage || '';

    function bindGalleryThumbs() {
      if (!gallery || !mainImage) {
        return;
      }

      gallery.querySelectorAll('.gallery-thumb').forEach(function (thumb) {
        thumb.addEventListener('click', function () {
          var full = thumb.getAttribute('data-full');
          if (full) {
            mainImage.src = full;
          }
          gallery.querySelectorAll('.gallery-thumb').forEach(function (image) {
            image.classList.remove('active-thumb', 'border-primary');
          });
          thumb.classList.add('active-thumb', 'border-primary');
        });
      });
    }

    function updateFavoriteState(isFavorite) {
      if (!favoriteButton) {
        return;
      }
      favoriteButton.classList.toggle('btn-danger', Boolean(isFavorite));
      favoriteButton.classList.toggle('btn-outline-danger', !isFavorite);
      favoriteButton.textContent = isFavorite ? '❤️ Saved' : '🤍 Save';
    }

    function renderImages(payload) {
      var coverUrl = payload.cover_image_url || placeholderImage;
      if (mainImage) {
        mainImage.src = coverUrl;
        mainImage.alt = payload.prop_title || 'Property';
      }

      if (!gallery) {
        return;
      }

      var items = [];
      if (payload.cover_image_url) {
        items.push(
          '<div class="col">' +
            '<img src="' + escapeHtml(payload.cover_image_url) + '" class="img-fluid rounded border gallery-thumb active-thumb border-primary" style="height: 90px; width: 100%; object-fit: cover; cursor:pointer;" alt="Cover image" data-full="' + escapeHtml(payload.cover_image_url) + '">' +
          '</div>'
        );
      }

      (payload.gallery_images || []).forEach(function (image, index) {
        items.push(
          '<div class="col">' +
            '<img src="' + escapeHtml(image.image_url) + '" class="img-fluid rounded border gallery-thumb" style="height: 90px; width: 100%; object-fit: cover; cursor:pointer;" alt="Property image ' + String(index + 2) + '" data-full="' + escapeHtml(image.image_url) + '">' +
          '</div>'
        );
      });

      gallery.innerHTML = items.join('');
      gallery.classList.toggle('d-none', items.length === 0);
      bindGalleryThumbs();
    }

    function pollDetails() {
      return fetchJson(detail.dataset.pollUrl).then(function (payload) {
        if (title) {
          title.textContent = payload.prop_title || '';
        }
        if (price) {
          price.textContent = payload.prop_price || '';
        }
        if (location) {
          location.textContent = [payload.prop_location || '', payload.prop_state || ''].filter(Boolean).join(', ');
        }
        if (description) {
          description.textContent = payload.prop_desc || '';
        }
        if (address) {
          address.textContent = payload.prop_address || '';
        }
        renderImages(payload);
        updateFavoriteState(payload.is_favorite);
      });
    }

    bindGalleryThumbs();
    registerPoller({ interval: 15000, onTick: pollDetails, immediate: false });
  }

  function initializeFavoriteButtons() {
    var button = document.querySelector('[data-favorite-toggle-url]');
    if (!button) {
      return;
    }

    button.addEventListener('click', function () {
      if (button.dataset.khLoading === 'true') {
        return;
      }

      setButtonLoading(button, true);
      fetchJson(button.dataset.favoriteToggleUrl, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      })
        .then(function (payload) {
          var isFavorite = Boolean(payload.is_favorite);
          var nextLabel = isFavorite ? '❤️ Saved' : '🤍 Save';
          var state = buttonState.get(button);
          if (state) {
            state.html = nextLabel;
          }
          button.classList.toggle('btn-danger', isFavorite);
          button.classList.toggle('btn-outline-danger', !isFavorite);
          button.textContent = nextLabel;
        })
        .catch(function (error) {
          console.error('Favorite toggle failed:', error);
        })
        .finally(function () {
          setButtonLoading(button, false);
        });
    });
  }

  function initializeProfileStats() {
    var panel = document.querySelector('[data-profile-stats-url]');
    if (!panel) {
      return;
    }

    function setText(id, value) {
      var node = document.getElementById(id);
      if (node) {
        node.textContent = value;
      }
    }

    function pollProfileStats() {
      return fetchJson(panel.dataset.profileStatsUrl).then(function (payload) {
        setText('profileTotalProperties', String(payload.total_properties || 0));
        setText('profileActiveListings', String(payload.active_listings || 0));
        setText('profileFavoritesCount', String(payload.favorites_count || 0));
        setText('profileUnreadMessages', String(payload.unread_messages || 0));
        if (payload.views_count !== null && payload.views_count !== undefined) {
          setText('profileViewsCount', String(payload.views_count || 0));
        }
      });
    }

    registerPoller({ interval: 10000, onTick: pollProfileStats, immediate: false });
  }

  function initializeAdminDashboardStats() {
    var panel = document.querySelector('[data-admin-stats-url]');
    if (!panel) {
      return;
    }

    function setText(id, value) {
      var node = document.getElementById(id);
      if (node) {
        node.textContent = value;
      }
    }

    function pollAdminStats() {
      return fetchJson(panel.dataset.adminStatsUrl).then(function (payload) {
        setText('adminTotalProperties', String(payload.total_properties || 0));
        setText('adminActiveListings', String(payload.active_listings || 0));
        setText('adminUsersCount', String(payload.users_count || 0));
        setText('adminFavoritesCount', String(payload.favorites_count || 0));
        setText('adminContactMessagesCount', String(payload.contact_messages_count || 0));
        setText('adminAdministratorsCount', String(payload.administrators_count || 0));
        if (payload.pending_approvals !== null && payload.pending_approvals !== undefined) {
          setText('adminPendingApprovals', String(payload.pending_approvals || 0));
        }

        var badge = document.getElementById('adminUnreadMessagesBadge');
        if (badge) {
          var unread = Number(payload.unread_messages_count || 0);
          badge.textContent = String(unread);
          badge.classList.toggle('d-none', unread <= 0);
        }
      });
    }

    registerPoller({ interval: 10000, onTick: pollAdminStats, immediate: false });
  }

  function initialize() {
    bindFormLoadingStates(document);
    initializeUnreadBadge();
    initializeChatPage();
    initializePropertiesPage();
    initializeMyListingsPage();
    initializePropertyDetailPage();
    initializeFavoriteButtons();
    initializeProfileStats();
    initializeAdminDashboardStats();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize);
  } else {
    initialize();
  }
})();
