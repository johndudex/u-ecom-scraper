"""Deterministic navigation exploration node.

Replaces the free-form LLM navigation agent with a fixed procedure:
  1. Load homepage (Playwright or web_fetch)
  2. Extract navigation structure (category links, search form, menus)
  3. Visit one category/search-result page
  4. Extract item-link pattern + pagination from that page
  5. Write raw findings to workspace/{slug}/navigation_findings.json

No LLM decision-making — every step is deterministic Python.  The LLM
synthesis happens in the downstream ``navigate_synthesize`` node.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

# ── Extraction scripts (run inside the browser via playwright_evaluate) ────

_HOMEPAGE_EXTRACTION_JS = r"""
() => {
  // Helper: extract clean text from an element, filtering out CSS garbage
  // (Sitegainer injects <style> tags inside elements, polluting textContent)
  function cleanText(el) {
    if (!el) return '';
    // Strategy 1: Try innerText (excludes <style>/<script> content)
    if (el.innerText) {
      const t = el.innerText.trim();
      if (t && t.length > 1 && t.length < 80 && !isCssGarbage(t)) return t;
    }
    // Strategy 2: Try child elements
    const children = el.querySelectorAll('span, p, div');
    for (const child of children) {
      const t = (child.innerText || child.textContent || '').trim();
      if (t && t.length > 1 && t.length < 80 && !isCssGarbage(t)) return t;
    }
    // Strategy 3: Clean the raw textContent
    const raw = (el.textContent || '').trim();
    return cleanCssFromText(raw);
  }
  function isCssGarbage(text) {
    if (!text) return true;
    // CSS rule patterns
    if (/^\./.test(text) && text.includes('{')) return true;
    if (/^(color|background|font|border|margin|padding|width|height|display|position):/.test(text)) return true;
    if (text.includes('{') && text.includes(':')) return true;
    if (text.length > 200) return true; // Too long for a nav label
    return false;
  }
  function cleanCssFromText(text) {
    if (!text) return '';
    // Remove CSS blocks: .classname { ... }
    let cleaned = text.replace(/\.[a-z0-9_]+\s*\{[^}]*\}/gi, '');
    // Remove standalone CSS declarations: property: value;
    cleaned = cleaned.replace(/(color|background|font|border|margin|padding|width|height|display|position)\s*:[^;]+;?/gi, '');
    cleaned = cleaned.replace(/\s+/g, ' ').trim();
    // If nothing left, try extracting the last word segment that looks like text
    if (!cleaned && text.length > 0) {
      const words = text.match(/[A-Za-z]{2,}/g);
      if (words && words.length > 0) return words.join(' ').substring(0, 80);
    }
    return cleaned.substring(0, 80);
  }

  const result = {
    category_links: [],
    search_form: null,
    nav_menus: [],
    footer_links: [],
    all_links_sample: [],
  };

  // --- STEP 0: Unhide mega menus / dropdowns / panels ---
  // Many sites hide nav panels with display:none, max-height:0, visibility:hidden,
  // or aria-hidden until hover/click. These are present in the DOM and we want to
  // extract links from them. Temporarily override these styles.
  const unhideSelectors = [
    '[style*="display: none"]',
    '[style*="display:none"]',
    '[style*="max-height: 0"]',
    '[style*="max-height:0"]',
    '[style*="visibility: hidden"]',
    '[style*="visibility:hidden"]',
    '[aria-hidden="true"]',
  ];
  const unhidden = [];
  unhideSelectors.forEach(sel => {
    document.querySelectorAll(sel).forEach(el => {
      // Only unhide elements inside nav/menu containers
      if (el.closest('nav, [role="navigation"], .menu, .navbar, .header-nav, ' +
          '.main-nav, .category-nav, .categories, .primary-nav, .site-nav, ' +
          'header, .mega-menu, .dropdown-menu, .mega-nav')) {
        const prev = {
          display: el.style.display,
          maxHeight: el.style.maxHeight,
          visibility: el.style.visibility,
          overflow: el.style.overflow,
        };
        el.style.setProperty('display', 'block', 'important');
        el.style.setProperty('max-height', 'none', 'important');
        el.style.setProperty('visibility', 'visible', 'important');
        el.style.setProperty('overflow', 'visible', 'important');
        unhidden.push({ el, prev });
      }
    });
  });

  // Also click all dropdown/mega-menu triggers to reveal panels
  document.querySelectorAll(
    '.dropdown-toggle, .mega-nav__trigger, [data-toggle="dropdown"], ' +
    '[aria-expanded="false"].dropdown, button[aria-haspopup="true"], ' +
    'li.has-dropdown, .nav-item.has-children'
  ).forEach(t => {
    try { t.click(); } catch(e) {}
    try { t.setAttribute('aria-expanded', 'true'); } catch(e) {}
  });

  // --- Find category-like links ---
  const navContainers = document.querySelectorAll(
    'nav, [role="navigation"], .menu, .navbar, .header-nav, .main-nav, ' +
    '.category-nav, .categories, .primary-nav, .site-nav, ' +
    'header ul, header ol, .mega-menu, .dropdown-menu, ' +
    '.mega-nav, .mega-nav__panel, .utility-bar'
  );

  navContainers.forEach(container => {
    const links = container.querySelectorAll('a[href]');
    const menuText = (container.textContent || '').trim().substring(0, 100);
    const menuInfo = { text: menuText, links: [] };
    links.forEach(a => {
      const href = a.href;
      const text = cleanText(a);
      if (href && text && text.length > 1 &&
          !href.startsWith('#') && !href.startsWith('javascript:') &&
          !href.startsWith('mailto:') && !href.startsWith('tel:')) {
        result.category_links.push({ href, text });
        menuInfo.links.push({ href, text });
      }
    });
    if (menuInfo.links.length > 0) {
      result.nav_menus.push(menuInfo);
    }
  });

  // FALLBACK: If no nav containers matched (common on SPA/React sites that use
  // <div> with CSS module classes instead of semantic <nav>), scan ALL links.
  if (result.category_links.length === 0) {
    const allPageLinks = document.querySelectorAll('a[href]');
    const linkMap = {};
    allPageLinks.forEach(a => {
      const href = a.href;
      const text = cleanText(a);
      if (!href || !text || text.length < 2) return;
      if (href.startsWith('#') || href.startsWith('javascript:')) return;
      if (href.startsWith('mailto:') || href.startsWith('tel:')) return;
      // Skip social/media/auth links
      if (/facebook|twitter|instagram|tiktok|youtube|linkedin|pinterest/i.test(href)) return;
      if (/\/login|\/signin|\/register|\/cart|\/wishlist|\/account/i.test(href)) return;
      result.category_links.push({ href, text });
    });
  }

  // Deduplicate category links
  const seen = new Set();
  result.category_links = result.category_links.filter(l => {
    if (seen.has(l.href)) return false;
    seen.add(l.href);
    return true;
  }).slice(0, 25);

  // nav_menus duplicates category_links — keep only a summary (top 3 menus)
  result.nav_menus = result.nav_menus.slice(0, 3).map(m => ({
    text: m.text,
    link_count: m.links.length,
  }));

  // --- Find search form ---
  const searchInput = document.querySelector(
    'input[type="search"], input[name*="search" i], input[name*="q" i], ' +
    'input[placeholder*="search" i], input[aria-label*="search" i], ' +
    '#search-box, .search-input, .ae-searchbar__input, .site-search-text'
  );
  const searchForm = searchInput ?
    searchInput.closest('form') :
    document.querySelector('form[action*="search" i]');

  if (searchForm) {
    const inputs = Array.from(searchForm.querySelectorAll('input, select, textarea'))
      .map(i => ({
        tag: i.tagName,
        type: i.type || '',
        name: i.name || '',
        id: i.id || '',
        placeholder: i.placeholder || '',
        value: i.type === 'hidden' ? (i.value || '').substring(0, 50) : '',
      }));
    result.search_form = {
      action: searchForm.action || '',
      method: (searchForm.method || 'get').toLowerCase(),
      inputs,
      search_input_name: searchInput ? (searchInput.name || searchInput.id || 'q') : 'q',
      search_input_selector: searchInput ?
        (searchInput.id ? '#' + searchInput.id :
         searchInput.name ? 'input[name="' + searchInput.name + '"]' : 'input[type="search"]')
        : null,
      has_action_url: !!(searchForm.action && searchForm.action.indexOf('javascript') === -1),
    };
  } else if (searchInput) {
    result.search_form = {
      action: null,
      method: null,
      search_input_selector: searchInput.id ? '#' + searchInput.id : 'input[type="search"]',
      note: 'Search input found but no enclosing form — likely JS-driven search',
    };
  }

  // --- Detect URL-based search patterns from links ---
  const searchUrlPatterns = [];
  const searchLinkRegexes = [
    /\/search\?/i, /\/search\//i, /search\.aspx/i,
    /\?search=/i, /\?q=/i, /\?keyword=/i, /\?query=/i, /\?searchterm=/i,
  ];
  result.all_links_sample.forEach(href => {
    if (searchLinkRegexes.some(re => re.test(href))) {
      if (searchUrlPatterns.length < 5) searchUrlPatterns.push(href);
    }
  });
  if (searchUrlPatterns.length > 0) {
    result.search_url_hints = searchUrlPatterns;
  }

  // --- Collect a sample of all links (for URL pattern detection) ---
  const allLinks = document.querySelectorAll('a[href]');
  const linkSample = [];
  allLinks.forEach(a => {
    const href = a.href;
    if (href && !href.startsWith('#') && !href.startsWith('javascript:')) {
      linkSample.push(href);
    }
  });
  result.all_links_sample = linkSample.slice(0, 40);

  // --- Footer links (often contain sitemap, category links) ---
  document.querySelectorAll('footer a[href], .footer a[href]').forEach(a => {
    const href = a.href;
    const text = (a.textContent || '').trim();
    if (href && text && text.length > 1 && text.length < 60) {
      result.footer_links.push({ href, text });
    }
  });
  result.footer_links = result.footer_links.slice(0, 15);

  // --- Extract button-based navigation items ---
  // Some sites (Next.js, React, Material-UI) use <button> for nav instead of <a>.
  // These buttons trigger client-side routing. We capture their labels and
  // try to infer URLs from common patterns.
  const navButtons = document.querySelectorAll(
    'nav button[aria-haspopup="true"], ' +
    'nav button.MuiButtonBase-root, ' +
    '.navigationBar button, ' +
    'header button[role="menuitem"], ' +
    'nav button[class*="nav" i]'
  );
  const buttonNavItems = [];
  navButtons.forEach(btn => {
    const text = (btn.textContent || '').trim();
    const ariaLabel = btn.getAttribute('aria-label') || '';
    const label = text || ariaLabel;
    if (label && label.length > 1 && label.length < 50) {
      // Check if the button has a link inside or nearby
      const innerLink = btn.querySelector('a[href]');
      if (innerLink) {
        result.category_links.push({ href: innerLink.href, text: label });
      } else {
        buttonNavItems.push({ text: label, ariaLabel });
      }
    }
  });
  if (buttonNavItems.length > 0) {
    result.nav_buttons = buttonNavItems.slice(0, 20);
  }

  // Deduplicate category links again (may have added from buttons)
  const seen2 = new Set();
  result.category_links = result.category_links.filter(l => {
    if (seen2.has(l.href)) return false;
    seen2.add(l.href);
    return true;
  }).slice(0, 25);

  // --- Detect SPA frameworks for rendering hints ---
  result.framework_hints = {};
  if (document.querySelector('#__next, [data-reactroot], #__react-root')) {
    result.framework_hints.spa = true;
    if (document.querySelector('#__next')) result.framework_hints.framework = 'nextjs';
  }
  if (document.querySelector('.MuiGrid-root, .MuiContainer-root, [class*="MuiCard"]')) {
    result.framework_hints.ui_library = 'material_ui';
  }

  // --- Restore hidden elements ---
  unhidden.forEach(({ el, prev }) => {
    el.style.display = prev.display;
    el.style.maxHeight = prev.maxHeight;
    el.style.visibility = prev.visibility;
    el.style.overflow = prev.overflow;
  });

  return JSON.stringify(result);
}
"""

_LISTING_PAGE_EXTRACTION_JS = r"""
() => {
  // Helper: clean CSS garbage from text (Sitegainer pattern)
  function cleanText(el) {
    if (!el) return '';
    // Strategy 1: Try innerText (excludes <style>/<script> content)
    if (el.innerText) {
      const t = el.innerText.trim();
      if (t && t.length > 1 && t.length < 120 && !isCssGarbage(t)) return t;
    }
    // Strategy 2: Try child elements
    const children = el.querySelectorAll('span, p, div, h2, h3, a');
    for (const child of children) {
      const t = (child.innerText || child.textContent || '').trim();
      if (t && t.length > 1 && t.length < 120 && !isCssGarbage(t)) return t;
    }
    // Strategy 3: Clean the raw textContent
    const raw = (el.textContent || '').trim();
    return cleanCssFromText(raw);
  }
  function isCssGarbage(text) {
    if (!text) return true;
    if (/^\./.test(text) && text.includes('{')) return true;
    if (/^(color|background|font|border|margin|padding|width|height|display|position):/.test(text)) return true;
    if (text.includes('{') && text.includes(':')) return true;
    if (text.length > 200) return true;
    return false;
  }
  function cleanCssFromText(text) {
    if (!text) return '';
    let cleaned = text.replace(/\.[a-z0-9_]+\s*\{[^}]*\}/gi, '');
    cleaned = cleaned.replace(/(color|background|font|border|margin|padding|width|height|display|position)\s*:[^;]+;?/gi, '');
    cleaned = cleaned.replace(/\s+/g, ' ').trim();
    if (!cleaned && text.length > 0) {
      const words = text.match(/[A-Za-z]{2,}/g);
      if (words && words.length > 0) return words.join(' ').substring(0, 120);
    }
    return cleaned.substring(0, 120);
  }

  const result = {
    product_links: [],
    pagination: null,
    item_count_text: null,
    grid_containers: [],
    page_count: null,
    total_products: null,
  };

  // --- Detect item/product links ---
  // Strategy 1: data-cy / data-product attributes (most reliable)
  const cardSelectors = [
    '[data-cy="product-grid-item"]',
    "[data-product-id]",
    "[data-pid]",
    "div.product[data-pid]",
    "[data-sku]",
    ".product-card",
    ".product-item",
    ".item-card",
    ".product-tile",
    ".ae-plp-card",
    ".c-grid-item",
    // MUI / Next.js patterns
    ".MuiCard-root",
    '[class*="ProductCard"]',
    '[class*="product-card"]',
    '[class*="product-tile"]',
    '[class*="item-card"]',
    '[class*="book-card"]',
    '[class*="BookCard"]',
    '[class*="BookTile"]',
    // NOTE: [data-productid] is intentionally last — on SFCC sites it matches
    // the TurnTo rating widget (.TTteaser), not the product card itself.
    "[data-productid]",
  ];

    let detectedViaCardSelector = false;
    for (const sel of cardSelectors) {
      const cards = document.querySelectorAll(sel);
      if (cards.length >= 3) {
        detectedViaCardSelector = true;
        const seen = new Set();
        cards.forEach((card, i) => {
          if (i >= 200) return;
        const link = card.querySelector('a[href]');
        if (!link) return;
        const href = link.href;
        if (!href || seen.has(href) || href.startsWith('#')) return;
        seen.add(href);
        const text = (link.getAttribute('data-productname') ||
                     link.getAttribute('title') ||
                     cleanText(card) || '').trim().substring(0, 120);
        // Extract data attributes for richer info
        const cardData = { href, text };
        const dataAttrs = [
          "data-sku",
          "data-productid",
          "data-product-id",
          "data-pid",
          "data-brand",
          "data-price",
          "data-productname",
          "data-productcategoryid",
        ];
        for (const attr of dataAttrs) {
          const val = link.getAttribute(attr) || card.getAttribute(attr);
          if (val) cardData[attr.replace("data-", "").replace(/-/g, "_")] = val;
        }
        // Also try the wishlist button inside the card (adameve pattern)
        const skuBtn = card.querySelector('[data-sku]');
        if (skuBtn && !cardData.sku) {
          cardData.sku = skuBtn.getAttribute('data-sku');
        }
        result.product_links.push(cardData);
      });
      result.grid_containers.push({
        selector: sel,
        card_count: cards.length,
      });
      break;
    }
  }

  // Strategy 2: grid grouping by parent class
  // ONLY used if Strategy 1 didn't find cards. Filters out category-like links.
  if (result.product_links.length < 3) {
    // Helper: does this link look like a product (not a category/nav link)?
    const looksLikeProduct = (href, text) => {
      if (!href || !text) return false;
      // Reject short text (likely nav: "Home", "Cart", "Sale")
      if (text.length < 5) return false;
      // Reject category URL patterns
      if (/-ch-\d+/.test(href) || /\/c(?:ategory)?\//i.test(href)) return false;
      if (/\/collections\//i.test(href) || /\/browse\//i.test(href)) return false;
      // Accept product URL patterns
      if (/\/sp-/.test(href) || /\/product\//i.test(href) || /\/p\//i.test(href)) return true;
      if (/\/item\//i.test(href) || /\/pd\//i.test(href) || /\/dp\//i.test(href)) return true;
      if (/\/book\//i.test(href)) return true;
      if (/-c\.aspx$/.test(href) || /-c\.html$/.test(href)) return true;
      // Accept slug-based product URLs with embedded product codes (e.g. CK UK: /watch-ck-pulse-wf25100063000)
      if (/\/[a-z]+-[a-z]+-[\w-]*\d{4,}[\w-]*$/.test(new URL(href).pathname)) return true;
      // Accept if text is long enough and URL has a product-like path (3+ segments)
      if (text.length > 15 && new URL(href).pathname.split('/').length >= 3) return true;
      return false;
    };

    const allLinks = Array.from(document.querySelectorAll('a[href]'));
    const linkCounts = {};
    allLinks.forEach(a => {
      const href = a.href;
      if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
      const text = cleanText(a);
      if (!looksLikeProduct(href, text)) return;
      let parent = a.parentElement;
      let parentClass = '';
      for (let i = 0; i < 4 && parent; i++) {
        if (parent.className && typeof parent.className === 'string') {
          parentClass = parent.className.split(' ')[0];
          if (parentClass) break;
        }
        parent = parent.parentElement;
      }
      const key = parentClass || 'no-class';
      if (!linkCounts[key]) linkCounts[key] = [];
      linkCounts[key].push({ href, text, parentClass: key });
    });
    let bestKey = null;
    let bestCount = 0;
    for (const [key, links] of Object.entries(linkCounts)) {
      if (links.length > bestCount) {
        bestCount = links.length;
        bestKey = key;
      }
    }
    if (bestKey && bestCount >= 3) {
      result.product_links = linkCounts[bestKey].slice(0, 200).map(l => ({
        href: l.href, text: l.text,
      }));
      result.grid_containers.push({
        parent_class: bestKey,
        link_count: bestCount,
      });
    }
  }

  // Strategy 3: URL pattern matching for product links only
  if (result.product_links.length < 3) {
    const productLinkSelectors = [
      'a[href*="/sp-"]', 'a[href*="/product/"]', 'a[href*="/p/"]',
      'a[href*="/item/"]', 'a[href*="/pd/"]', 'a[href*="/dp/"]',
      'a[href*="/book/"]',
      '.product-card a', '.product-item a', '.item-card a',
      '[data-product-id] a', '.ae-plp-card a', '.ae-plp-card__link',
      '.MuiCard-root a[href]', '[class*="ProductCard"] a[href]',
    ];
    const found = new Set();
    for (const sel of productLinkSelectors) {
      document.querySelectorAll(sel).forEach(a => {
        const href = a.href;
        if (href && !found.has(href)) {
          found.add(href);
          result.product_links.push({
            href, text: cleanText(a).substring(0, 100)
          });
        }
      });
    }
    result.product_links = result.product_links.slice(0, 200);
  }

  // --- Detect pagination ---
  const nextLink = document.querySelector(
    'a[rel="next"], .pagination .next, .next-page, ' +
    'a[aria-label*="next" i], .page-next, li.next a, ' +
    '#load-more-component, [id^="load-more"] a, ' +
    '#plp-button a'
  );
  const pageNumbers = document.querySelectorAll(
    '.pagination a, .page-numbers a, .pager a, ' +
    '.pagination .page, [class*="pagenum"] a'
  );
  let loadMoreBtn = document.querySelector(
    'button[class*="load-more" i], a[class*="load-more" i], ' +
    '.show-more, [class*="showmore" i], ' +
    '#load-more-component, #load-more-wrapper a, ' +
    '#plp-button a, .ae-plp__button a, ' +
    'button[class*="show-more" i], a[class*="show-more" i], ' +
    'button[class*="ShowMore" i], button[class*="loadMore" i], ' +
    'button[aria-label*="show more" i], button[aria-label*="load more" i]'
  );

  // Exclude facet/filter "show more" (SearchSpring .ss-facet-show-more, etc.)
  if (loadMoreBtn && loadMoreBtn.closest(
    '.ss-facets, .ss-facet-group, .ss-facet-values, ' +
    '[class*="facet" i], [class*="filter" i], #facets, .refinements'
  )) {
    loadMoreBtn = null;
  }

  // Text-based "Show More" detection (for sites where the button has no
  // distinguishing class — e.g. Material-UI buttons with just label text)
  if (!loadMoreBtn) {
    const allButtons = document.querySelectorAll('button, a[role="button"]');
    for (const btn of allButtons) {
      const text = (btn.textContent || '').trim().toLowerCase();
      if (text === 'show more' || text === 'load more' || text === 'view more' ||
          text === 'see more' || text === 'show all') {
        result._show_more_btn = true;
        break;
      }
    }
  }

  // Detect AJAX data-attribute based load-more (adameve pattern)
  const ajaxLoadMore = document.querySelector(
    '[data-ajax-href-value], [data-controller="ajax"][data-action*="load-more"]'
  );

  if (nextLink && nextLink.href) {
    // Check if this is actually a load-more anchor (adameve uses <a> for load more)
    const isLoadMore = nextLink.closest(
      '#load-more-wrapper, #load-more-component, #plp-button, ' +
      '.ae-plp__button, [id^="load-more"]'
    );
    if (isLoadMore) {
      result.pagination = {
        type: 'load_more',
        selector: '#load-more-component, .ae-plp__button a',
        next_href: nextLink.href,
      };
    } else {
      result.pagination = {
        type: 'next_button',
        next_selector: 'a[rel="next"]',
        next_href: nextLink.href,
      };
    }
  } else if (pageNumbers.length > 0) {
    result.pagination = {
      type: 'page_numbers',
      sample_hrefs: Array.from(pageNumbers).slice(0, 5).map(a => a.href),
    };
  } else if (loadMoreBtn) {
    result.pagination = {
      type: 'load_more',
      selector: loadMoreBtn.id ? '#' + loadMoreBtn.id :
                (loadMoreBtn.className ? '.' + loadMoreBtn.className.split(' ')[0] : ''),
      next_href: loadMoreBtn.href || '',
    };
  } else if (ajaxLoadMore) {
    result.pagination = {
      type: 'load_more',
      selector: '[data-ajax-href-value]',
      next_href: ajaxLoadMore.getAttribute('data-ajax-href-value') || '',
    };
  } else if (result._show_more_btn) {
    result.pagination = {
      type: 'load_more',
      selector: 'button',
      next_href: '',
      note: 'Show More button detected by text content',
    };
    delete result._show_more_btn;
  }

  // Check URL-based pagination (?page=, &pnum=, ?start=, ?sz=)
  const url = window.location.href;
  const pageParamMatch = url.match(/[?&](page|p|pnum|pg|pn)=(\d+)/i);
  if (pageParamMatch) {
    result.pagination = result.pagination || {};
    result.pagination.page_param = pageParamMatch[1];
    result.pagination.url_pattern = "url_with_" + pageParamMatch[1] + "_param";
  }

  // SFCC offset pagination (?start=0&sz=24)
  const startMatch = url.match(/[?&]start=(\d+)/i);
  const szMatch = url.match(/[?&]sz=(\d+)/i);
  if (startMatch) {
    result.pagination = result.pagination || {};
    result.pagination.type = result.pagination.type || "offset_param";
    result.pagination.page_param = "start";
    result.pagination.page_size_param = "sz";
    result.pagination.page_size = szMatch ? parseInt(szMatch[1], 10) : 24;
    result.pagination.url_pattern = "url_with_start_sz_params";
  }

  // Detect <link rel="next"> in <head> (SFCC, WordPress, etc.)
  if (!result.pagination) {
    const linkNext = document.querySelector('link[rel="next"]');
    if (linkNext && linkNext.href) {
      result.pagination = {
        type: "next_button",
        next_href: linkNext.href,
      };
    }
  }

  // Detect numbered pagination buttons (Fredhopper, React apps with hashed classes)
  // Look for 3+ buttons with purely numeric text content inside the product area
  if (!result.pagination) {
    const allBtns = document.querySelectorAll('button, a[role="button"]');
    const numericBtns = [];
    allBtns.forEach(b => {
      const t = (b.textContent || '').trim();
      if (t && /^\d+$/.test(t)) numericBtns.push({el: b, page: parseInt(t, 10)});
    });
    if (numericBtns.length >= 3) {
      const maxPage = Math.max(...numericBtns.map(b => b.page));
      result.pagination = {
        type: "page_numbers",
        max_pages: maxPage,
        note: "Numbered buttons detected (Fredhopper or SPA pagination)",
      };
    }
  }

  // Detect Fredhopper / Shopify liquid pagination config
  try {
    if (window.liquidCustom && window.liquidCustom.pagination) {
      const pConfig = window.liquidCustom.pagination;
      if (!result.pagination) {
        result.pagination = {
          type: pConfig.infiniteScrollMode === "button" ? "load_more" : "infinite_scroll",
          items_per_page: pConfig.itemsPerPage || null,
        };
      }
      result.framework_config = {fredhopper: true, shopify: true};
    }
  } catch(e) {}

  // Detect Fredhopper platform
  if (window.fredhopper) {
    result.framework_config = result.framework_config || {};
    result.framework_config.fredhopper = true;
  }

  // Detect "next page" / "previous page" text links (CK UK, some SFCC sites)
  // These are plain <a> or <span> elements with text like "next page", "previous page"
  // Often accompanied by a page indicator like "01/02" or "1 of 2"
  if (!result.pagination) {
    const allLinks = document.querySelectorAll('a, span, button, div');
    for (const el of allLinks) {
      const t = (el.textContent || '').trim().toLowerCase();
      if (t === 'next page' || t === 'next') {
        result.pagination = {
          type: 'next_button',
          next_selector: 'a, span, button, div',
          next_text: t,
          next_href: el.href || '',
          note: 'next page text link detected',
        };
        break;
      }
    }
  }

  // Detect page indicators like "01/02", "1 of 2", "1/2" near pagination
  if (!result.pagination) {
    const pageIndicatorRegex = /(\d{1,2})\s*(?:\/|of)\s*(\d{1,3})/;
    const allTexts = document.querySelectorAll(
      'a, span, div, p, li, [class*="pagination" i], [class*="pager" i], [class*="page" i]'
    );
    for (const el of allTexts) {
      const t = (el.textContent || '').trim();
      const match = t.match(pageIndicatorRegex);
      if (match) {
        const current = parseInt(match[1], 10);
        const total = parseInt(match[2], 10);
        if (total >= 2 && current < total) {
          result.pagination = {
            type: 'page_numbers',
            current_page: current,
            max_pages: total,
            page_indicator_text: t.trim(),
            note: 'Page indicator detected (e.g. 01/02)',
          };
          break;
        }
      }
    }
  }

  // --- Extract page count (total pages) ---
  const pageCountInput = document.querySelector(
    'input[name="page-count"], input[name="pageCount"], ' +
    'input[name="total-pages"], [data-page-count]'
  );
  if (pageCountInput) {
    const val = pageCountInput.value || pageCountInput.getAttribute('data-page-count');
    if (val && /^\d+$/.test(val)) {
      result.page_count = parseInt(val, 10);
      if (result.pagination) {
        result.pagination.max_pages = result.page_count;
      }
    }
  }

  // --- Extract total product count ---
  const totalEl = document.querySelector(
    '#products_total, [id*="products_total"], ' +
    '[id*="product-count"], [id*="totalCount"], ' +
    '.total-products, .result-count'
  );
  if (totalEl) {
    const text = (totalEl.textContent || '').trim();
    const match = text.match(/\d+/);
    if (match) {
      result.total_products = parseInt(match[0], 10);
    }
  }

  // Fallback: parse "N items" / "N results" / "N products" from body text
  if (!result.total_products) {
    const bodyText = (document.body ? document.body.innerText : '');
    const countPatterns = [
      /(\d+)\s*(?:items?|results?|products?|found|available)/i,
      /(?:showing|displaying)\s*\d+(?:\s*[-–to]\s*\d+)?\s*(?:of)\s*(\d+)/i,
      /(\d+)\s*(?:of)\s*(\d+)\s*(?:items?|results?|products?)/i,
    ];
    for (const pat of countPatterns) {
      const m = bodyText.match(pat);
      if (m) {
        // Prefer the "of N" number (total), otherwise the first number
        result.total_products = parseInt(m[m.length - 1], 10);
        break;
      }
    }
  }

  // --- Item count text ---
  const countElements = document.querySelectorAll(
    '.results-count, .product-count, .item-count, ' +
    '[class*="result-count"], [class*="product-count"], ' +
    '[class*="showing"], [class*="total"], ' +
    '.ae-plp__counter, [class*="items-found" i], ' +
    '[class*="search-results" i] h1, [class*="search-results" i] h2, ' +
    'h1[class*="title" i], [class*="page-title" i], ' +
    '[class*="listing-header" i], [class*="plp-header" i]'
  );
  countElements.forEach(el => {
    const text = (el.textContent || '').trim();
    if (text && text.length < 200 && /\d/.test(text)) {
      result.item_count_text = text;
    }
  });

  return JSON.stringify(result);
}
"""


# ── Helper functions ───────────────────────────────────────────────────────


def _read_site_analysis(root: str, slug: str) -> dict[str, Any]:
    """Read site_analysis.json for connectivity info."""
    path = os.path.join(root, "workspace", slug, "site_analysis.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("navigate_explore: cannot read site_analysis.json: %s", exc)
        return {}


def _get_tool_by_name(tools: list, name: str):
    """Find a tool by name in a list of LangChain BaseTool."""
    for t in tools:
        if getattr(t, "name", "") == name:
            return t
    return None


def _invoke_tool(tool, **kwargs) -> str:
    """Invoke a LangChain tool synchronously and return its string output."""
    if tool is None:
        return "ERROR: tool not available"
    try:
        result = tool.invoke(kwargs)
        if hasattr(result, "content"):
            return str(result.content)
        return str(result)
    except Exception as exc:
        logger.error(
            "navigate_explore: tool %s failed: %s", getattr(tool, "name", "?"), exc
        )
        return f"ERROR: {exc}"


def _persist_explore_summary(job_id: int, findings: dict) -> None:
    """Write a summary SessionLog entry for the agent summary page."""
    if not job_id:
        return
    try:
        from scraper.models import SessionLog

        cats = findings.get("homepage_nav", {}).get("category_links", [])
        prods = findings.get("listing_page", {}).get("product_links", [])
        errors = findings.get("errors", [])
        search_form = findings.get("homepage_nav", {}).get("search_form")
        url_patterns = findings.get("url_patterns", {})
        pagination = findings.get("listing_page", {}).get("pagination", {})

        method = findings.get("method", "unknown")
        summary = (
            f"Navigation exploration complete\n"
            f"  Method: {method}\n"
            f"  Categories found: {len(cats)}\n"
            f"  Product links found: {len(prods)}\n"
            f"  Errors: {len(errors)}\n"
        )
        if search_form:
            summary += f"  Search form: action={search_form.get('action')}, input={search_form.get('search_input_selector')}\n"
        if url_patterns:
            for pattern, info in url_patterns.items():
                summary += f"  URL pattern [{pattern}]: {info.get('count', '?')} matches\n"
        if pagination:
            summary += f"  Pagination: type={pagination.get('type', 'unknown')}, total={pagination.get('total_product_count', '?')}\n"
        if errors:
            summary += f"  Errors: {', '.join(str(e)[:80] for e in errors[:5])}\n"
        if prods:
            sample_urls = [p.get("href", "") for p in prods[:5]]
            summary += f"  Sample product URLs:\n"
            for u in sample_urls:
                summary += f"    - {u}\n"

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_ASSISTANT,
            agent="navigation-explore",
            content=summary,
            seq=seq,
        )
    except Exception as exc:
        logger.warning("navigate_explore: failed to persist summary log: %s", exc)


def _parse_eval_json(raw: str) -> dict:
    """Extract a JSON object from a Playwright MCP evaluate tool result.

    The MCP ``browser_evaluate`` tool wraps results in markdown::
        ### Result
        "{\\"hello\\":\\"world\\"}"
        ### Ran Playwright code
        ...

    This helper extracts the JSON string from between the quotes and parses it.
    Falls back to direct parsing if the wrapper isn't found.
    """
    if not raw or not isinstance(raw, str):
        return {}

    # Strategy 1: Extract from ### Result section
    result_match = re.search(r"### Result\s*\n(.+?)(?:\n###|\Z)", raw, re.DOTALL)
    if result_match:
        json_str = result_match.group(1).strip()
        # The MCP tool wraps string results in quotes
        if json_str.startswith('"') and json_str.endswith('"'):
            # Unescape the outer quotes and parse the inner JSON
            try:
                inner = json.loads(json_str)  # This handles the outer string
                if isinstance(inner, str):
                    return json.loads(inner)
                return inner if isinstance(inner, dict) else {}
            except (json.JSONDecodeError, TypeError):
                pass
        # Try direct parse
        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            pass

    # Strategy 2: Find the first valid JSON object in the raw string
    # Look for { ... } patterns
    for i, ch in enumerate(raw):
        if ch == "{":
            # Try to parse from here, finding the matching close brace
            depth = 0
            for j in range(i, len(raw)):
                if raw[j] == "{":
                    depth += 1
                elif raw[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[i : j + 1])
                        except (json.JSONDecodeError, TypeError):
                            break

    return {}


def _detect_url_patterns(links: list[dict], base_url: str) -> dict[str, Any]:
    """Analyze a list of {href, text} link dicts to find URL patterns."""
    if not links:
        return {}

    paths = []
    for link in links:
        try:
            parsed = urlparse(link.get("href", ""))
            if parsed.path and parsed.path != "/":
                paths.append(parsed.path)
        except Exception:
            pass

    if not paths:
        return {}

    # Find common suffixes (e.g., -c.aspx, .html, /product/)
    suffixes: dict[str, int] = {}
    for path in paths:
        # Last segment
        last_segment = path.rstrip("/").rsplit("/", 1)[-1]
        # Check for common product page indicators
        for pattern in [
            r"-c\.aspx$",
            r"-ch\d+\.aspx$",  # adameve category homepage
            r"-c\.html$",
            r"\.aspx$",
            r"\.html$",
            r"\.htm$",
            r"/product/",
            r"/p/",
            r"/item/",
            r"/pd/",
            r"/dp/",
            r"/sp-",  # adameve product page prefix
        ]:
            if re.search(pattern, last_segment, re.IGNORECASE):
                suffixes[pattern] = suffixes.get(pattern, 0) + 1

    best_suffix = max(suffixes, key=suffixes.get) if suffixes else None

    return {
        "detected_suffix_pattern": best_suffix,
        "sample_paths": paths[:10],
        "total_unique_paths": len(set(paths)),
    }


def _is_non_category_link(href: str, text: str) -> bool:
    """Filter out non-category links (privacy, terms, auth, social, etc.)."""
    href_lower = href.lower()
    text_lower = (text or "").lower()
    non_category_patterns = [
        "privacy",
        "terms",
        "policy",
        "tos",
        "agreement",
        "login",
        "signin",
        "register",
        "signup",
        "account",
        "cart",
        "wishlist",
        "checkout",
        "facebook",
        "twitter",
        "instagram",
        "tiktok",
        "youtube",
        "linkedin",
        "pinterest",
        "mailto:",
        "tel:",
        "unsubscribe",
        "cookie",
        "gdpr",
        "ccpa",
        "help",
        "support",
        "contact",
        "faq",
        "about",
        "careers",
        "press",
        "stores",
        "store-locator",
        "directions",
        "maps.google",
        "shipping",
        "returns",
        "track",
        "order",
        "#main",
        "#skip",
        "javascript:",
    ]
    return any(p in href_lower or p in text_lower for p in non_category_patterns)


def _pick_category_to_visit(
    category_links: list[dict],
    search_criteria: str,
    base_url: str,
) -> str | None:
    """Pick the best category link to visit for item-link extraction.

    Prefers categories whose text matches the search criteria.
    Falls back to the first category link that looks like a listing page.
    """
    if not category_links:
        return None

    # Filter out non-category links
    category_links = [
        link
        for link in category_links
        if not _is_non_category_link(link.get("href", ""), link.get("text", ""))
    ]

    if not category_links:
        return None

    criteria_lower = search_criteria.lower().strip()
    if criteria_lower:
        # Score by keyword overlap
        criteria_words = set(criteria_lower.split())
        for link in category_links:
            text_lower = (link.get("text") or "").lower()
            href_lower = (link.get("href") or "").lower()
            if any(word in text_lower or word in href_lower for word in criteria_words):
                return link["href"]

    # Fall back: look for links that look like category pages (not product pages)
    for link in category_links:
        href = link.get("href", "")
        path = urlparse(href).path
        # Heuristic: category pages often have -ch-, /category/, /c/, /collections/
        if re.search(
            r"(-ch-|/c(?:ategory)?/|/collections/|/shop/|/browse/|-ch\d+)",
            path,
            re.IGNORECASE,
        ):
            return href

    # Fall back: look for short path links (SPA category pages like /fiction, /kids)
    for link in category_links:
        href = link.get("href", "")
        path = urlparse(href).path
        if path and path != "/" and len(path.strip("/").split("/")) == 1:
            return href

    # Last resort: first link
    return category_links[0].get("href")


def _construct_category_urls_from_buttons(
    nav_buttons: list[dict],
    base_url: str,
) -> list[dict]:
    """Construct category URLs from button-based nav labels.

    For SPA sites where nav items are <button> (no href), we infer URLs from
    common patterns: /{slug}, /category/{slug}, /collections/{slug}.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    result: list[dict] = []

    for btn in nav_buttons:
        label = btn.get("text") or btn.get("ariaLabel") or ""
        if not label or len(label) > 50:
            continue
        # Slugify the label
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        if not slug:
            continue
        # Generate candidate URLs
        for pattern in [f"/{slug}", f"/category/{slug}", f"/collections/{slug}"]:
            result.append(
                {"href": f"{origin}{pattern}", "text": label, "inferred": True}
            )

    return result


def _build_search_urls(
    search_form: dict | None,
    search_criteria: str,
    base_url: str,
    homepage_data: dict,
) -> list[str]:
    """Construct candidate search results URLs.

    Returns multiple patterns to try — the caller should attempt each until
    one returns actual product links.
    """
    if not search_criteria:
        return []
    from urllib.parse import quote as url_quote

    # Split comma-separated criteria into separate search terms
    terms = [t.strip() for t in search_criteria.split(",") if t.strip()]

    all_candidates: list[str] = []
    for term in terms:
        criteria_encoded = url_quote(term, safe="")
        candidates: list[str] = []

        # If the form has a real action URL, use it
        if (
            search_form
            and search_form.get("action")
            and search_form["action"] != "null"
        ):
            action = search_form["action"]
            if not action.startswith(("javascript:", "#")):
                param = search_form.get("search_input_name", "q")
                separator = "&" if "?" in action else "?"
                candidates.append(f"{action}{separator}{param}={criteria_encoded}")

        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        base_path = parsed.path.rstrip("/")

        # Look for search URL hints from the homepage links
        url_hints = homepage_data.get("search_url_hints", [])
        for hint in url_hints:
            hint_parsed = urlparse(hint)
            if hint_parsed.query:
                import urllib.parse as up

                params = up.parse_qs(hint_parsed.query)
                for key in list(params.keys()):
                    if key.lower() in ("q", "search", "keyword", "query", "kw", "searchterm"):
                        params[key] = [term]
                new_query = up.urlencode(params, doseq=True)
                candidates.append(up.urlunparse(hint_parsed._replace(query=new_query)))

        criteria_slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
        common_patterns = [
            f"{origin}{base_path}/search?q={criteria_encoded}",
            f"{origin}{base_path}/search?search={criteria_encoded}",
            f"{origin}{base_path}/search?searchterm={criteria_encoded}",
            f"{origin}{base_path}/search?searchTerm={criteria_encoded}",
            f"{origin}/search?q={criteria_encoded}",
            f"{origin}/search?search={criteria_encoded}",
            f"{origin}/search?searchterm={criteria_encoded}",
            f"{origin}/search?searchTerm={criteria_encoded}",
            f"{origin}{base_path}/search.aspx?search={criteria_encoded}",
            f"{origin}{base_path}/search.asp?search={criteria_encoded}",
            f"{origin}{base_path}/search/?q={criteria_encoded}",
            f"{origin}{base_path}/search/{criteria_encoded}",
            f"{origin}{base_path}/search/{criteria_slug}",
            f"{origin}/search/{criteria_encoded}",
        ]
        candidates.extend(common_patterns)
        all_candidates.extend(candidates)

    # Deduplicate while preserving order
    seen: set[str] = set()
    return [u for u in all_candidates if not (u in seen or seen.add(u))]


# Keep old name as alias for backward compat
def _build_search_url(
    search_form: dict | None,
    search_criteria: str,
    base_url: str,
    homepage_data: dict,
) -> str | None:
    urls = _build_search_urls(search_form, search_criteria, base_url, homepage_data)
    return urls[0] if urls else None


_WAIT_FOR_CONTENT_JS = r"""
() => {
  // Detect Cloudflare challenge
  const cf = document.querySelector('#challenge-running, #challenge-form, .cf-browser-verification');
  if (cf) return JSON.stringify({cloudflare: true});

  // Check if page has h1 or title (basic page loaded signal)
  const h1 = document.querySelector('h1');
  if (!h1 && !document.title) return JSON.stringify({loaded: false});

  return JSON.stringify({loaded: true, cloudflare: false});
}
"""

# Selectors that indicate product content has rendered
_PRODUCT_PRESENCE_SELECTORS = [
    '[data-cy="product-grid-item"]',
    "[data-product-id]",
    "[data-productid]",
    "[data-pid]",
    ".product-card",
    ".product-item",
    ".product-tile",
    ".ae-plp-card",
    "div.product[data-pid]",
    '[class*="ProductCard"]',
    '[class*="product-card"]',
    '[class*="book-card"]',
    '[class*="BookCard"]',
    '.MuiCard-root a[href*="/book/"]',
    'a[href*="/book/"]',
    'a[href*="/product/"]',
    'a[href*="/sp-"]',
    'a[href*="/p/"]',
    'button[class*="add-to-cart" i]',
    'button[class*="addToCart" i]',
]


def _wait_for_content(
    evaluate,
    timeout: int = 25,
    poll_interval: float = 2.0,
) -> dict:
    """Poll the page until product content appears or timeout.

    Returns a dict with ``loaded`` and ``cloudflare`` keys.
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check for Cloudflare
        check_raw = _invoke_tool(evaluate, function=_WAIT_FOR_CONTENT_JS)
        check = _parse_eval_json(check_raw)
        if check.get("cloudflare"):
            logger.warning("navigate_explore: Cloudflare challenge detected")
            return {"loaded": False, "cloudflare": True}

        # Check for product presence
        presence_js = f"""
        () => {{
            const sels = {json.dumps(_PRODUCT_PRESENCE_SELECTORS)};
            for (const sel of sels) {{
                if (document.querySelectorAll(sel).length >= 3) {{
                    return JSON.stringify({{present: true, selector: sel}});
                }}
            }}
            return JSON.stringify({{present: false}});
        }}
        """
        presence_raw = _invoke_tool(evaluate, function=presence_js)
        presence = _parse_eval_json(presence_raw)
        if presence.get("present"):
            logger.info(
                "navigate_explore: content detected via %s", presence.get("selector")
            )
            return {
                "loaded": True,
                "cloudflare": False,
                "selector": presence.get("selector"),
            }

        time.sleep(poll_interval)

    # Generic fallback: check if page has many links sharing a product-like
    # URL pattern (e.g., /product/, cod-, /item/).  Works on sites that use
    # hashed CSS classes where named selectors don't match.
    generic_js = r"""
    () => {
    const links = Array.from(document.querySelectorAll('a[href]'));
    const urlSet = new Set();
    for (const a of links) {
        const href = a.href || '';
        if (/\/product\/|\/item\/|cod-|\/sp-|\/p\//i.test(href) ||
                (/\d{4,}/.test(href) && href.split('/').length >= 4) ||
                /\/[a-z]+-[a-z]+-[\w-]*\d{4,}/i.test(href)) {
            urlSet.add(href.split('?')[0].split('#')[0]);
        }
    }
        }
        const unique = urlSet.size;
        return JSON.stringify({present: unique >= 3, count: unique});
    }
    """
    generic_raw = _invoke_tool(evaluate, function=generic_js)
    generic = _parse_eval_json(generic_raw)
    if generic.get("present"):
        logger.info(
            "navigate_explore: content detected via generic link pattern (count=%d)",
            generic.get("count"),
        )
        return {
            "loaded": True,
            "cloudflare": False,
            "selector": "generic_link_pattern",
        }

    logger.warning("navigate_explore: content wait timed out after %ds", timeout)
    return {"loaded": False, "cloudflare": False}


def _visit_and_extract(
    navigate,
    evaluate,
    page_url: str,
    page_label: str,
    findings: dict,
) -> str | None:
    """Navigate to a URL, wait for content, extract listing page data.

    Handles both SSR and CSR sites by polling for product card selectors.
    Returns the visited URL on success, None on failure.
    """
    logger.info("navigate_explore: visiting listing page %s", page_url)
    nav_result = _invoke_tool(navigate, url=page_url)
    findings["listing_page"]["url"] = page_label
    findings["listing_page"]["navigate_result"] = nav_result[:500]

    # Check for Cloudflare challenge in nav result
    if "challenge" in nav_result.lower() or "cloudflare" in nav_result.lower():
        findings["errors"].append(
            "Cloudflare challenge detected — content may not have loaded"
        )

    # Wait for content to render (handles CSR sites like Next.js, React, Vue)
    content_status = _wait_for_content(evaluate, timeout=25)
    if content_status.get("cloudflare"):
        findings["errors"].append("Cloudflare challenge blocked content extraction")
        # Still try extraction — sometimes the challenge auto-resolves
    elif not content_status.get("loaded"):
        # Content not detected via selectors — try anyway after a brief wait
        import time

        time.sleep(5)

    listing_data_raw = _invoke_tool(
        evaluate,
        function=_LISTING_PAGE_EXTRACTION_JS,
    )
    listing_data = _parse_eval_json(listing_data_raw)
    if not listing_data:
        findings["errors"].append(f"Failed to parse listing extraction for {page_url}")

    findings["listing_page"].update(listing_data)
    product_count = len(listing_data.get("product_links", []))
    logger.info(
        "navigate_explore: extracted %d product links from %s",
        product_count,
        page_url,
    )
    return page_url


def _try_form_search(
    navigate,
    evaluate,
    search_form: dict,
    search_criteria: str,
    findings: dict,
) -> None:
    """Try submitting the search form by typing into the input and submitting."""
    findings["search_attempted"] = True
    search_selector = search_form.get("search_input_selector")
    if not search_selector:
        return

    logger.info(
        "navigate_explore: trying form-based search via %s for '%s'",
        search_selector,
        search_criteria,
    )
    escaped = search_criteria.replace("'", "\\'").replace("\n", " ")
    eval_js = f"""
    () => {{
        const input = document.querySelector('{search_selector}');
        if (input) {{
            input.value = '{escaped}';
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            const form = input.closest('form');
            if (form) {{
                form.submit();
                return 'submitted';
            }}
            // Try pressing Enter
            input.dispatchEvent(new KeyboardEvent('keydown', {{
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
            }}));
            return 'enter_pressed';
        }}
        return 'input not found';
    }}
    """
    search_result = _invoke_tool(evaluate, function=eval_js)
    findings["homepage_nav"]["search_submit_result"] = search_result[:200]

    # Wait for content to render (handles CSR sites like Next.js, React, Vue)
    import time

    content_status = _wait_for_content(evaluate, timeout=25)
    if not content_status.get("loaded"):
        time.sleep(5)

    listing_data_raw = _invoke_tool(
        evaluate,
        function=_LISTING_PAGE_EXTRACTION_JS,
    )
    listing_data = _parse_eval_json(listing_data_raw)

    findings["listing_page"].update(listing_data)


_PROMO_URL_KEYWORDS = [
    "special-collection", "pride-collection", "bestsellers", "sale-",
    "gift", "edit", "new-arrivals", "new-in",
]


def _has_real_product_links(findings: dict) -> bool:
    product_links = findings.get("listing_page", {}).get("product_links", [])
    if len(product_links) < 3:
        return False
    real = [
        p for p in product_links
        if not any(kw in (p.get("href", "") or "").lower() for kw in _PROMO_URL_KEYWORDS)
    ]
    if len(real) < 3:
        return False
    cat_hrefs = {
        (c.get("href", "") or "").lower()
        for c in findings.get("homepage_nav", {}).get("category_links", [])
    }
    if cat_hrefs:
        product_only = [p for p in real if (p.get("href", "") or "").lower() not in cat_hrefs]
        return len(product_only) >= 3
    return True


def _try_interactive_pagination(evaluate, findings: dict) -> None:
    """Try clicking Load More / Next Page / infinite scroll to get more products."""
    import time

    listing = findings.get("listing_page", {})
    product_links = listing.get("product_links", [])
    if not product_links:
        return

    pagination_info = listing.get("pagination") or {}
    total_count = (
        listing.get("total_products", 0)
        or pagination_info.get("total_product_count", 0)
        or pagination_info.get("max_pages", 0)
    )
    current_count = len(product_links)

    logger.info(
        "navigate_explore: pagination — have %d products, total=%s",
        current_count,
        total_count or "unknown",
    )

    dismiss_js = r"""() => {
        const consentTexts = [
            'allow all', 'accept all', 'accept', 'i agree', 'agree',
            'got it', 'ok', 'continue', 'yes', 'sure',
            'allow', 'consent', 'approve',
        ];
        const btns = document.querySelectorAll('button, a[role="button"], a[class*="consent" i], button[class*="consent" i]');
        for (const b of btns) {
            const t = (b.textContent || '').trim().toLowerCase();
            if (consentTexts.some(ct => t === ct || t.startsWith(ct))) {
                if (b.offsetParent !== null) {
                    b.click();
                    return 'dismissed: ' + t;
                }
            }
        }
        return 'no_consent';
    }"""

    max_rounds = 5
    scroll_attempts = 0
    for round_num in range(max_rounds * 2):
        time.sleep(2)

        # Dismiss cookie consent that may appear after navigation
        _invoke_tool(evaluate, function=dismiss_js)

        pagination_js = r"""() => {
            // 1. Load More button
            const loadMore = document.querySelector(
                'button[class*="load-more" i], a[class*="load-more" i], '
                + 'button[data-action="load-more"], .load-more-btn, '
                + 'button[class*="show-more" i], button[class*="view-more" i]'
            );
            if (loadMore && loadMore.offsetParent !== null) {
                loadMore.click();
                return JSON.stringify({action: 'clicked_load_more'});
            }

            // 2. Next page link (standard)
            const next = document.querySelector(
                'a[rel="next"], .pagination .next, button.next, '
                + 'a[aria-label="Next" i], a[aria-label="next" i], '
                + 'li.next a, .pager .next a'
            );
            if (next) {
                next.click();
                return JSON.stringify({action: 'clicked_next_page'});
            }

            // 3. "next page" text link (CK UK style and similar SFCC sites)
            const allClickable = document.querySelectorAll(
                'a, button, [role="button"], [tabindex="0"]'
            );
            for (const el of allClickable) {
                const t = (el.textContent || '').trim().toLowerCase();
                if (t === 'next page' || t === 'next >' || t === 'next ›') {
                    el.click();
                    return JSON.stringify({action: 'clicked_next_page_text'});
                }
            }

            // 4. Infinite scroll — scroll to bottom
            window.scrollTo(0, document.body.scrollHeight);
            return JSON.stringify({action: 'scrolled_to_bottom'});
        }"""
        action_raw = _invoke_tool(evaluate, function=pagination_js)
        action = _parse_eval_json(action_raw)

        if not action:
            break

        action_type = action.get("action", "")
        logger.info(
            "navigate_explore: pagination round %d — %s",
            round_num + 1,
            action_type,
        )

        if action_type == "scrolled_to_bottom":
            time.sleep(4)
            scroll_attempts += 1
            if scroll_attempts >= 3:
                logger.info("navigate_explore: infinite scroll — 3 scrolls with no new products, stopping")
                break
        else:
            time.sleep(3)
            scroll_attempts = 0

        content_status = _wait_for_content(evaluate, timeout=10)
        if not content_status.get("loaded"):
            time.sleep(2)

        new_data_raw = _invoke_tool(
            evaluate,
            function=_LISTING_PAGE_EXTRACTION_JS,
        )
        new_data = _parse_eval_json(new_data_raw)
        if not new_data:
            break

        new_links = new_data.get("product_links", [])
        existing_hrefs = {p.get("href", "") for p in product_links}
        added = [p for p in new_links if p.get("href", "") not in existing_hrefs]

        if added:
            scroll_attempts = 0
            product_links.extend(added)
            listing["product_links"] = product_links
            if new_data.get("pagination"):
                listing["pagination"] = new_data["pagination"]

            logger.info(
                "navigate_explore: pagination round %d — added %d products (total %d)",
                round_num + 1,
                len(added),
                len(product_links),
            )
        elif action_type != "scrolled_to_bottom":
            logger.info(
                "navigate_explore: pagination round %d — no new products, stopping",
                round_num + 1,
            )
            break

        if total_count and len(product_links) >= total_count:
            logger.info(
                "navigate_explore: pagination — reached total count (%d >= %d)",
                len(product_links),
                total_count,
            )
            break


def _do_explore_via_browser(
    tools: list,
    base_url: str,
    search_criteria: str,
    site_analysis: dict,
    search_url: str = "",
) -> dict[str, Any]:
    """Run the exploration procedure using Playwright MCP tools.

    Search-first approach:
    1. If search_url provided, skip homepage and go directly to that page.
    2. Navigate to homepage → dismiss cookies → extract search form + locale.
    3. PRIMARY: Type into search box, press Enter (interactive form search).
    4. SECONDARY: If no search form, try URL-based search patterns.
    5. If products found, try interactive pagination (Load More, Next, scroll).
    6. FALLBACK: If search fails, try category links from homepage.
    7. Detect URL patterns.
    """
    navigate = _get_tool_by_name(tools, "playwright_browser_navigate")
    evaluate = _get_tool_by_name(tools, "playwright_browser_evaluate")

    findings: dict[str, Any] = {
        "method": "playwright",
        "homepage_url": base_url,
        "homepage_nav": {},
        "listing_page": {},
        "search_attempted": False,
        "errors": [],
    }

    import time

    # ── Shortcut: if search_url provided, skip homepage ───────────────────
    if search_url:
        logger.info(
            "navigate_explore: search_url provided, skipping homepage: %s", search_url
        )
        _visit_and_extract(navigate, evaluate, search_url, search_url, findings)
        if _has_real_product_links(findings):
            logger.info(
                "navigate_explore: search_url yielded %d products",
                len(findings["listing_page"]["product_links"]),
            )
            _try_interactive_pagination(evaluate, findings)
            _detect_and_save_url_patterns(findings, None, base_url)
            return findings
        logger.info(
            "navigate_explore: search_url yielded no real products, falling back to homepage"
        )
        findings["listing_page"] = {}

    # ── STEP 1: Navigate to homepage ────────────────────────────────────
    logger.info("navigate_explore: STEP 1 — loading homepage %s", base_url)
    nav_result = _invoke_tool(navigate, url=base_url)
    findings["homepage_nav"]["navigate_result"] = nav_result[:500]

    # STEP 1b: Dismiss cookie consent / GDPR dialog if present
    time.sleep(3)
    dismiss_js = r"""() => {
        const consentTexts = [
            'allow all', 'accept all', 'accept', 'i agree', 'agree',
            'got it', 'ok', 'continue', 'yes', 'sure',
            'allow', 'consent', 'approve',
        ];
        const btns = document.querySelectorAll('button, a[role="button"], a[class*="consent" i], button[class*="consent" i]');
        for (const b of btns) {
            const t = (b.textContent || '').trim().toLowerCase();
            if (consentTexts.some(ct => t === ct || t.startsWith(ct))) {
                if (b.offsetParent !== null) {
                    b.click();
                    return 'dismissed: ' + t;
                }
            }
        }
        return 'no consent dialog found';
    }"""
    dismiss_result = _invoke_tool(evaluate, function=dismiss_js)
    if "dismissed" in dismiss_result:
        logger.info("navigate_explore: cookie consent %s", dismiss_result[:100])
        time.sleep(3)

    # ── STEP 2: Extract homepage navigation structure ────────────────────
    logger.info("navigate_explore: STEP 2 — extracting homepage nav structure")
    homepage_data_raw = _invoke_tool(
        evaluate,
        function=_HOMEPAGE_EXTRACTION_JS,
    )
    homepage_data = _parse_eval_json(homepage_data_raw)
    if not homepage_data:
        findings["errors"].append(
            f"Failed to parse homepage extraction result (raw[:200]: {homepage_data_raw[:200]})"
        )

    findings["homepage_nav"].update(homepage_data)

    # STEP 2b: Detect locale prefix
    locale_js = r"""() => {
        const path = window.location.pathname;
        const match = path.match(/^\/([a-z]{2}(?:-[a-z]{2,4})?)(?:\/|$)/i);
        if (match && match[1].length <= 7) return JSON.stringify({locale: match[1], prefix: '/' + match[1]});
        return JSON.stringify({locale: null, prefix: ''});
    }"""
    locale_raw = _invoke_tool(evaluate, function=locale_js)
    locale_info = _parse_eval_json(locale_raw)
    locale_prefix = locale_info.get("prefix", "") if locale_info else ""
    if locale_prefix:
        logger.info("navigate_explore: detected locale prefix %s", locale_prefix)
        findings["locale_prefix"] = locale_prefix

    # ── STEP 3: Search (form-based PRIMARY, URL-based SECONDARY) ────────
    category_links = homepage_data.get("category_links", [])
    search_form = homepage_data.get("search_form")
    # Build effective base URL with locale prefix (avoid double-prefix)
    if locale_prefix:
        effective_base_url = base_url.rstrip("/")
        # Strip existing locale suffix if present (base_url may already contain it)
        if effective_base_url.endswith(locale_prefix):
            pass  # Already has locale, no need to add
        else:
            effective_base_url = effective_base_url + locale_prefix
    else:
        effective_base_url = base_url

    found_products = False

    # 3a: PRIMARY — Interactive form-based search (type into search box + Enter)
    form_search_count = 0
    if search_criteria and search_form and search_form.get("search_input_selector"):
        logger.info(
            "navigate_explore: STEP 3a — interactive form search for '%s'",
            search_criteria,
        )
        _try_form_search(navigate, evaluate, search_form, search_criteria, findings)
        found_products = _has_real_product_links(findings)
        form_search_count = len(
            findings.get("listing_page", {}).get("product_links", [])
        )
        logger.info(
            "navigate_explore: form search found %d products", form_search_count
        )

    # 3b: SECONDARY — URL-based search
    # Also try when form search worked to compare — form search may use a
    # different/lower-result endpoint (e.g. search.aspx vs /search?searchTerm=)
    if search_criteria and (not found_products or form_search_count < 30):
        findings["search_attempted"] = True
        search_urls = _build_search_urls(
            search_form, search_criteria, effective_base_url, homepage_data
        )
        if not search_urls:
            logger.info("navigate_explore: no search URLs could be built")
        for idx, surl in enumerate(search_urls[:6]):
            logger.info(
                "navigate_explore: trying search URL %d/%d: %s",
                idx + 1,
                min(len(search_urls), 6),
                surl,
            )
            prev_listing = findings.get("listing_page", {}).copy()
            findings["listing_page"] = {}
            _visit_and_extract(navigate, evaluate, surl, surl, findings)
            new_count = len(
                findings.get("listing_page", {}).get("product_links", [])
            )
            if new_count > form_search_count:
                logger.info(
                    "navigate_explore: URL search found %d products (better than form's %d)",
                    new_count,
                    form_search_count,
                )
                found_products = True
                break
            if new_count > 0 and not found_products:
                found_products = True

    # 3c: Try clicking a search trigger button first, then form search
    # Always attempt if 3a+3b failed — hidden inputs (e.g. CK UK) cause silent 3a failure
    if search_criteria and not found_products:
        # Navigate back to homepage for search trigger/form access
        logger.info("navigate_explore: STEP 3c — navigating to homepage for search access")
        homepage_url = effective_base_url if effective_base_url else base_url
        _invoke_tool(navigate, url=homepage_url)
        time.sleep(3)

        trigger_js = r"""() => {
            const triggers = document.querySelectorAll(
                'button[aria-label*="search" i], a[aria-label*="search" i], '
                + '.search-toggle, .search-trigger, [data-toggle="search"], '
                + '[class*="search-icon" i], [class*="search-button" i], '
                + '[class*="SearchIcon" i]'
            );
            let clicked = false;
            for (const t of triggers) {
                t.click();
                clicked = true;
            }
            return clicked ? 'clicked_search_trigger' : 'no_trigger';
        }"""
        trigger_result = _invoke_tool(evaluate, function=trigger_js)
        logger.info(
            "navigate_explore: STEP 3c — trigger click result: %s",
            repr(trigger_result)[:300],
        )
        if "clicked" in str(trigger_result):
            time.sleep(2)
            # Re-extract homepage data to find the now-visible search form
            homepage_data_raw2 = _invoke_tool(
                evaluate, function=_HOMEPAGE_EXTRACTION_JS
            )
            homepage_data2 = _parse_eval_json(homepage_data_raw2)
            search_form2 = homepage_data2.get("search_form") if homepage_data2 else None
            if not search_form2 or not search_form2.get("search_input_selector"):
                logger.info(
                    "navigate_explore: form re-extraction failed, trying known selectors"
                )
                search_form2 = {
                    "search_input_selector": "input[name='searchTerm']",
                }
            logger.info(
                "navigate_explore: STEP 3c — form selector: %s",
                search_form2.get("search_input_selector"),
            )
            _try_form_search(
                navigate, evaluate, search_form2, search_criteria, findings
            )
            if _has_real_product_links(findings):
                found_products = True

    # ── STEP 4: Interactive pagination ──────────────────────────────────
    if found_products:
        _try_interactive_pagination(evaluate, findings)

    # ── STEP 5: Fallback — category exploration ──────────────────────────
    if not found_products:
        filtered_cats = [
            link
            for link in category_links
            if not _is_non_category_link(link.get("href", ""), link.get("text", ""))
        ]

        def _cat_priority(link):
            href = link.get("href", "")
            path = urlparse(href).path
            if search_criteria:
                criteria_words = set(search_criteria.lower().split())
                text_lower = (link.get("text") or "").lower()
                href_lower = href.lower()
                if any(w in text_lower or w in href_lower for w in criteria_words):
                    return 0
            if re.search(
                r"(-ch-|/c(?:ategory)?/|/collections/|/shop/|/browse/|-ch\d+)",
                path,
                re.IGNORECASE,
            ):
                return 1
            if path and path != "/" and len(path.strip("/").split("/")) == 1:
                return 2
            return 3

        filtered_cats.sort(key=_cat_priority)

        for cat in filtered_cats[:4]:
            cat_url = cat.get("href", "")
            if not cat_url:
                continue
            logger.info("navigate_explore: trying category %s", cat_url)
            findings["listing_page"] = {}
            _visit_and_extract(navigate, evaluate, cat_url, cat_url, findings)
            if _has_real_product_links(findings):
                found_products = True
                break

    if not found_products and not category_links:
        nav_buttons = homepage_data.get("nav_buttons", [])
        if nav_buttons:
            logger.info(
                "navigate_explore: trying %d inferred category URLs from button nav",
                len(nav_buttons),
            )
            inferred_cats = _construct_category_urls_from_buttons(nav_buttons, base_url)
            for cat in inferred_cats[:6]:
                findings["listing_page"] = {}
                _visit_and_extract(
                    navigate, evaluate, cat["href"], cat["href"], findings
                )
                if _has_real_product_links(findings):
                    found_products = True
                    findings["homepage_nav"]["category_links"].append(cat)
                    break

    if not found_products:
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        prefix = locale_prefix or ""
        listing_candidates = [
            f"{origin}{prefix}/books",
            f"{origin}{prefix}/browse",
            f"{origin}{prefix}/shop",
            f"{origin}{prefix}/shop-all",
            f"{origin}{prefix}/all-products",
            f"{origin}{prefix}/products",
            f"{origin}/books",
            f"{origin}/browse",
            f"{origin}/shop",
            f"{origin}/products",
        ]
        for listing_url in listing_candidates:
            findings["listing_page"] = {}
            _visit_and_extract(navigate, evaluate, listing_url, listing_url, findings)
            if _has_real_product_links(findings):
                found_products = True
                break

    if not found_products and not category_links and not search_form:
        findings["errors"].append(
            "No category links found and no search form available — "
            "cannot determine navigation patterns"
        )

    # ── STEP 6: Detect URL patterns ─────────────────────────────────────
    _detect_and_save_url_patterns(
        findings, homepage_data.get("category_links", []), base_url
    )

    return findings


def _detect_and_save_url_patterns(
    findings: dict, links_or_list: list | dict | None, base_url: str
) -> None:
    """Detect and save URL patterns from collected links."""
    all_links: list = []
    if isinstance(links_or_list, dict):
        all_links.extend(links_or_list.get("category_links", []))
    elif isinstance(links_or_list, list):
        all_links.extend(links_or_list)
    listing_links = findings.get("listing_page", {}).get("product_links", [])
    all_links.extend(listing_links)

    url_patterns = _detect_url_patterns(all_links, base_url)
    if url_patterns:
        findings["url_patterns"] = url_patterns


def _extract_json_ld(soup, base_url: str) -> dict[str, Any]:
    """Extract structured data from JSON-LD script tags.

    Handles ItemList, Product, and BreadcrumbList types.  Returns a dict
    with 'products' (list of product dicts) and 'breadcrumbs' (list of
    category names).
    """
    result: dict[str, Any] = {"products": [], "breadcrumbs": []}
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            schema_type = item.get("@type", "")
            if schema_type == "ItemList":
                elements = item.get("itemListElement", [])
                for elem in elements:
                    product = elem.get("item", {})
                    if not isinstance(product, dict) or not product.get("url"):
                        product = elem
                    url = product.get("url", "")
                    if not url:
                        continue
                    if url.startswith("/"):
                        url = urljoin(base_url, url)
                    entry: dict[str, Any] = {"href": url}
                    name = product.get("name", "")
                    if name:
                        entry["text"] = name
                    price_info = product.get("offers", {})
                    if isinstance(price_info, dict):
                        price = price_info.get("price", "")
                        if price:
                            try:
                                price_num = float(price) / 100
                                entry["price"] = f"${price_num:,.2f}"
                            except (ValueError, TypeError):
                                entry["price"] = price
                        currency = price_info.get("priceCurrency", "")
                        if currency:
                            entry["currency"] = currency
                    image = product.get("image", "")
                    if image:
                        entry["image"] = image
                    result["products"].append(entry)

            elif schema_type == "Product":
                url = item.get("url", "")
                if not url:
                    url = item.get("@id", "")
                if url:
                    if url.startswith("/"):
                        url = urljoin(base_url, url)
                    entry = {"href": url}
                    name = item.get("name", "")
                    if name:
                        entry["text"] = name
                    offer = item.get("offers", {})
                    if isinstance(offer, dict):
                        price = offer.get("price", "")
                        if price:
                            try:
                                price_num = float(price) / 100
                                entry["price"] = f"${price_num:,.2f}"
                            except (ValueError, TypeError):
                                entry["price"] = price
                        currency = offer.get("priceCurrency", "")
                        if currency:
                            entry["currency"] = currency
                    result["products"].append(entry)

            elif schema_type == "BreadcrumbList":
                elements = item.get("itemListElement", [])
                for elem in elements:
                    name = elem.get("name", "")
                    if name:
                        result["breadcrumbs"].append(name)

    seen_urls: set[str] = set()
    unique: list[dict] = []
    for p in result["products"]:
        href = p.get("href", "")
        if href and href not in seen_urls:
            seen_urls.add(href)
            unique.append(p)
    result["products"] = unique[:50]
    return result


def _looks_like_product_url(url: str) -> bool:
    """Filter out non-product URLs (category pages, promo pages, search pages).

    A product URL typically contains:
    - A product ID segment (numeric or alphanumeric with 4+ chars)
    - Path segments like /p/, /product/, /item/, /pd/, /dp/, /sku/
    - Long descriptive slugs with embedded product codes (SFCC, Shopify, etc.)
    - NOT just category/collection names, promo pages, or homepage sections
    """
    from urllib.parse import urlparse

    path = urlparse(url).path.lower()
    if not path or path == "/" or path == "/search":
        return False

    _non_product_segments = [
        "women", "men", "sale", "new-arrivals", "new-in", "bestsellers",
        "underwear", "swimwear", "jeans", "t-shirts", "dresses", "shoes",
        "bags", "accessories", "jackets", "outerwear", "sweatshirts",
        "hoodies", "lingerie", "nightwear", "socks", "shapewear",
        "special-collection", "pride-collection", "gift", "edit",
        "careers", "about", "contact", "help", "faq", "stores",
        "store-locator", "privacy", "terms", "cookie", "shipping",
        "returns", "track", "order", "newsletter", "account",
        "wishlist", "cart", "checkout", "search", "guide", "blog",
    ]
    segments = [s.strip("-") for s in path.strip("/").split("/") if s.strip("-")]
    if segments and all(
        any(s.startswith(seg) for seg in _non_product_segments)
        for s in segments
    ):
        return False

    product_patterns = [
        r'/[+-]p\w*\d{4,}',
        r'/[+-]p\d{4,}',
        r'/pid[-_]\d',
        r'/sku[-_]\d',
        r'/product[-_/]\w',
        r'/item[-_/]\w',
        r'/pd[-_/]\w',
        r'/dp[-_/]\w',
        r'-c\.aspx$',
        r'-c\.html$',
        r'/p/\w',
        r'/\d{6,}$',
        r'/[a-z0-9]+-[a-z0-9]{6,}$',
        r'/[a-z]+-[a-z]+-[\w-]*\d{4,}[\w-]*$',
    ]
    import re as _re
    for pat in product_patterns:
        if _re.search(pat, path):
            return True
    if len(path) > 20 and any(c.isdigit() for c in path):
        return True
    return False


def _extract_product_links_bs(soup, base_url: str) -> list[dict]:
    """Extract product links from a BeautifulSoup listing page.

    Uses the same strategy as the JS version: try card selectors first,
    then URL pattern matching, then generic link extraction.
    """
    from bs4 import Tag

    product_links: list[dict] = []
    seen: set[str] = set()

    # Strategy 0: JSON-LD ItemList (most reliable, works on any site with structured data)
    if soup and len(product_links) < 3:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string)
                if not isinstance(data, dict):
                    continue
                schema_type = data.get("@type", "")
                items = data.get("itemListElement", [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_url = item.get("url", "")
                    item_name = item.get("name", "")
                    if not item_url:
                        continue
                    if item_url.startswith("/"):
                        item_url = urljoin(base_url, item_url)
                    if item_url in seen:
                        continue
                    if schema_type == "ItemList" or _looks_like_product_url(item_url):
                        seen.add(item_url)
                        product_links.append({"href": item_url, "text": item_name[:100]})
                if len(product_links) >= 200:
                    break
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

    # Strategy 1: Card-based selectors (including SFCC/ecommerce platform-specific)
    card_selectors = [
        '[data-cy="product-grid-item"]',
        "[data-product-id]",
        "[data-productid]",
        "[data-sku]",
        ".product-card",
        ".product-item",
        ".item-card",
        ".product-tile",
        ".product-card--product",
        ".c-product-tile",
        "[ref=productTile]",
        "[data-tileid]",
        ".tile--product",
        "[data-testid=product-tile]",
        ".grid-item--product",
        ".plp-card",
        ".ae-plp-card",
        ".c-grid-item",
        '[class*="product-card"]',
        '[class*="product-tile"]',
        '[class*="product_tile"]',
        '[class*="plp-product"]',
        '[class*="grid-product"]',
        '[data-testid*="product" i]',
        '[data-testid*="tile" i]',
        '[data-testid*="card" i]',
        '[data-testid*="grid" i]',
    ]
    for sel in card_selectors:
        cards = soup.select(sel)
        if len(cards) >= 3:
            for card in cards[:200]:
                link = card.find("a", href=True) if isinstance(card, Tag) else None
                if not link:
                    continue
                href = urljoin(base_url, link["href"])
                if href in seen:
                    continue
                seen.add(href)
                text = (
                    link.get("data-productname", "")
                    or link.get("title", "")
                    or card.get_text(strip=True)[:120]
                    or link.get_text(strip=True)[:120]
                )
                card_data: dict = {"href": href, "text": text}
                # Extract data attributes
                for attr in [
                    "data-sku",
                    "data-productid",
                    "data-product-id",
                    "data-brand",
                    "data-price",
                    "data-productname",
                ]:
                    val = link.get(attr) or (
                        card.get(attr) if isinstance(card, Tag) else None
                    )
                    if val:
                        card_data[attr.replace("data-", "").replace("-", "_")] = val
                # Try wishlist button for SKU
                sku_btn = (
                    card.find(attrs={"data-sku": True})
                    if isinstance(card, Tag)
                    else None
                )
                if sku_btn and "sku" not in card_data:
                    card_data["sku"] = sku_btn.get("data-sku", "")
                product_links.append(card_data)
            if len(product_links) >= 3:
                break

    # Strategy 2: URL pattern selectors
    if len(product_links) < 3:
        pattern_selectors = [
            'a[href*="/p/"]',
            'a[href*="/product/"]',
            'a[href*="/item/"]',
            'a[href*="/pd/"]',
            'a[href*="/dp/"]',
            'a[href*="/sp-"]',
            'a[href*="-c.aspx"]',
            'a[href*="-c.html"]',
        ]
        for sel in pattern_selectors:
            for a in soup.select(sel):
                href = urljoin(base_url, a.get("href", ""))
                if href in seen or not _looks_like_product_url(href):
                    continue
                seen.add(href)
                text = a.get_text(strip=True)[:100]
                product_links.append({"href": href, "text": text})
            if len(product_links) >= 30:
                break

    # Strategy 2b: Regex pattern matching for product URLs not caught by selectors
    if len(product_links) < 3:
        import re as _re
        product_patterns = [
            r'/[+-]p\w*\d{4,}',       # -pK001, /p/387018890001, -pK00...
            r'/[+-]p\d{4,}',           # -p12345, /p/387018890001
            r'/pid[-_]\d',             # /pid-12345
            r'/sku[-_]\d',             # /sku-12345
            r'/product[-_/]\w',       # /product/abc123
        ]
        for a_tag in soup.select("a[href]"):
            href = a_tag.get("href", "")
            if not href:
                continue
            full_href = urljoin(base_url, href)
            if full_href in seen:
                continue
            for pat in product_patterns:
                if _re.search(pat, href):
                    seen.add(full_href)
                    text = a_tag.get_text(strip=True)[:100]
                    product_links.append({"href": full_href, "text": text})
                    break
            if len(product_links) >= 30:
                break

    # Strategy 3 was removed — returning garbage category links downstream
    # is worse than returning empty. If card selectors and URL patterns both
    # fail, the two-phase scraper discovers URLs at runtime.

    return product_links[:200]


def _extract_pagination_bs(soup, base_url: str, listing_page: dict) -> None:
    """Extract pagination info from BeautifulSoup listing page."""
    # Next button
    next_link = soup.select_one(
        'a[rel="next"], .pagination .next, .next-page, '
        "#load-more-component, [id^='load-more'] a"
    )
    if next_link and next_link.get("href"):
        # Check if load-more pattern
        is_load_more = next_link.find_parent(
            id=lambda x: x and "load-more" in str(x).lower()
        )
        if is_load_more:
            listing_page["pagination"] = {
                "type": "load_more",
                "selector": "#load-more-component",
                "next_href": urljoin(base_url, next_link["href"]),
            }
        else:
            listing_page["pagination"] = {
                "type": "next_button",
                "next_href": urljoin(base_url, next_link["href"]),
            }

    # Page numbers
    page_links = soup.select(".pagination a, .page-numbers a, .pager a")
    if page_links and "pagination" not in listing_page:
        listing_page["pagination"] = {
            "type": "page_numbers",
            "sample_hrefs": [
                urljoin(base_url, a.get("href", "")) for a in page_links[:5]
            ],
        }

    # Load more button
    load_more = soup.select_one(
        "button[class*='load-more' i], a[class*='load-more' i], "
        ".show-more, .ae-plp__button a"
    )
    if load_more and "pagination" not in listing_page:
        listing_page["pagination"] = {
            "type": "load_more",
            "next_href": urljoin(base_url, load_more.get("href", "")),
        }


def _fetch_via_probe_html(url: str) -> str:
    """Fetch page HTML via browser-service /render endpoint.

    Uses the correct access method (UC Chrome for Akamai sites, Playwright
    for JS-heavy sites, direct HTTP for simple sites).  This bypasses
    Playwright MCP entirely, making it work on Akamai-protected sites.
    """
    import httpx

    service_url = os.environ.get("BROWSER_SERVICE_URL", "http://browser-service:8001")
    from src.geo import detect_country as _detect_country

    country = _detect_country(url)

    try:
        from scraper.models import ProbeCache
        from django.utils import timezone
        from datetime import timedelta

        domain = urlparse(url).hostname or ""
        entry = ProbeCache.objects.filter(domain=domain).first()
        start_method = None
        if entry:
            expiry = entry.cached_at + timedelta(hours=4)
            if timezone.now() <= expiry:
                start_method = entry.method
    except Exception:
        start_method = None

    payload: dict = {"url": url, "timeout": 120}
    if start_method:
        payload["start_method"] = start_method
    if country:
        payload["country"] = country

    # Detect locale from URL path for Accept-Language header
    url_parsed = urlparse(url)
    locale_match = re.match(r"^(/[a-z]{2}-[a-z]{2,4}/)", url_parsed.path)
    if locale_match:
        payload["accept_language"] = locale_match.group(0).replace("/", "")  # "en-us"

    logger.info(
        "navigate_explore: probe_html fetch %s (method=%s, country=%s)",
        url[:100],
        start_method,
        country,
    )

    try:
        resp = httpx.post(
            f"{service_url}/render",
            json=payload,
            timeout=130,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            html = data.get("html", "")
            logger.info(
                "navigate_explore: probe_html success method=%s, len=%d",
                data.get("method", "?"),
                len(html),
            )
            return html

        logger.warning(
            "navigate_explore: probe_html failed: %s",
            data.get("error", "unknown"),
        )
        return f"RENDER FAILED: {data.get('error', 'unknown')}"
    except Exception as exc:
        logger.error("navigate_explore: probe_html error: %s", exc)
        return f"RENDER FAILED: {exc}"


def _needs_uc_chrome(site_analysis: dict) -> bool:
    """Check if the site analysis indicates UC Chrome is required.

    Returns True only if UC Chrome is the ONLY working method. If an
    http_method is available, Playwright MCP can also be used.
    """
    connectivity = site_analysis.get("connectivity", {})
    method = connectivity.get("method_that_worked", "")
    http_method = connectivity.get("http_method")
    if method.startswith("uc_chrome") and not http_method:
        return True
    if method.startswith("akamai"):
        return True
    if connectivity.get("needs_akamai_bypass"):
        return True
    return False


def _do_explore_via_http(
    base_url: str,
    search_criteria: str,
    site_analysis: dict,
    fetch_fn=None,
) -> dict[str, Any]:
    """Fallback: explore using web_fetch + BeautifulSoup (no JS rendering).

    When ``fetch_fn`` is provided, it is used instead of ``web_fetch`` to
    obtain page HTML.  This allows the same parsing logic to work with
    ``probe_html`` (UC Chrome) for Akamai-protected sites.
    """
    findings: dict[str, Any] = {
        "method": "http_requests",
        "homepage_url": base_url,
        "homepage_nav": {},
        "listing_page": {},
        "search_attempted": False,
        "errors": [],
    }

    if fetch_fn is None:
        from agents.tools.web_tools import get_web_tools

        web_tools = get_web_tools()
        web_fetch = _get_tool_by_name(web_tools, "web_fetch")
        fetch_fn = lambda u: _invoke_tool(web_fetch, url=u, format="text")
        findings["method"] = "http_requests"
    else:
        findings["method"] = "probe_html"

    # STEP 1: Fetch homepage
    logger.info("navigate_explore: STEP 1 (html) — fetching homepage %s", base_url)
    homepage_html = fetch_fn(base_url)

    if not homepage_html or homepage_html.startswith("RENDER FAILED"):
        findings["errors"].append(
            f"Homepage fetch failed: {homepage_html[:200] if homepage_html else 'empty response'}"
        )
        logger.error(
            "navigate_explore: homepage fetch returned error: %s",
            (homepage_html[:200] if homepage_html else "(empty)"),
        )
        return findings

    logger.info(
        "navigate_explore: homepage HTML len=%d, first 100 chars: %s",
        len(homepage_html),
        repr(homepage_html[:100]),
    )

    # Parse with BeautifulSoup
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(homepage_html[:500000], "html.parser")
    except Exception as exc:
        findings["errors"].append(f"Cannot parse homepage HTML: {exc}")
        return findings

    # Verify locale — check <html lang="..."> matches expected locale
    html_tag = soup.find("html")
    expected_locale = ""
    html_locale = ""
    if html_tag and html_tag.get("lang"):
        html_locale = html_tag["lang"].lower()
        parsed_base = urlparse(base_url)
        base_path = parsed_base.path.strip("/")
        if base_path and "-" in base_path:
            expected_locale = base_path.split("/")[0]
    if (
        expected_locale
        and html_locale
        and html_locale != expected_locale
        and not html_locale.startswith(expected_locale + "-")
        and not html_locale == expected_locale.split("-")[0]
    ):
        logger.info(
            "navigate_explore: LOCALE NOTE — expected '%s' but HTML is '%s'. "
            "Accepting as compatible locale variant.",
            expected_locale,
            html_locale,
        )
        findings["errors"].append(
            f"Locale variant: expected '{expected_locale}' but page is '{html_locale}' (compatible)"
        )

    # STEP 2: Extract navigation links (also look for hidden mega menu panels)
    category_links: list[dict] = []
    nav_containers = soup.select(
        "nav, [role=navigation], .menu, .navbar, .header-nav, "
        ".main-nav, .category-nav, .categories, header ul, "
        ".mega-menu, .dropdown-menu, .mega-nav, .utility-bar"
    )
    for container in nav_containers:
        for a in container.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            text = a.get_text(strip=True)
            if (
                href
                and text
                and 1 < len(text) < 80
                and not href.startswith(("#", "javascript:", "mailto:", "tel:"))
            ):
                category_links.append({"href": href, "text": text})

    # Deduplicate
    seen: set[str] = set()
    category_links = [
        link
        for link in category_links
        if link["href"] not in seen and not seen.add(link["href"])
    ][:50]

    # Fallback: if no nav containers matched (React/SPA with hashed classes),
    # scan ALL links and filter to internal category-like URLs.
    if not category_links:
        logger.info(
            "navigate_explore: no nav containers found, trying all-<a> fallback"
        )
        parsed_base = urlparse(base_url)
        for a in soup.find_all("a", href=True)[:300]:
            href = a["href"]
            full_href = urljoin(base_url, href)
            text = a.get_text(strip=True)
            if not text or len(text) < 2 or len(text) > 60:
                continue
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            parsed = urlparse(full_href)
            if parsed.hostname != parsed_base.hostname:
                continue
            if _is_non_category_link(full_href, text):
                continue
            category_links.append({"href": full_href, "text": text})
        seen2: set[str] = set()
        category_links = [
            link
            for link in category_links
            if link["href"] not in seen2 and not seen2.add(link["href"])
        ][:50]
        logger.info(
            "navigate_explore: all-<a> fallback found %d category links",
            len(category_links),
        )

    findings["homepage_nav"]["category_links"] = category_links

    # Find search form
    search_input = soup.select_one(
        "input[type=search], input[name*=search i], input[name*=q i], "
        "input[placeholder*=search i], .ae-searchbar__input, .site-search-text"
    )
    search_form_tag = None
    if search_input:
        search_form_tag = search_input.find_parent("form")
    else:
        search_form_tag = soup.select_one("form[action*=search i]")

    search_form_info = None
    if search_form_tag:
        search_form_info = {
            "action": search_form_tag.get("action", ""),
            "method": (search_form_tag.get("method") or "get").lower(),
            "search_input_name": (search_input.get("name") if search_input else "q"),
            "search_input_selector": None,
        }
        if search_input:
            if search_input.get("id"):
                search_form_info["search_input_selector"] = f"#{search_input['id']}"
            elif search_input.get("name"):
                search_form_info["search_input_selector"] = (
                    f"input[name='{search_input['name']}']"
                )
        findings["homepage_nav"]["search_form"] = search_form_info

    # STEP 3: Try search URLs first if criteria provided, then category pages
    visited = False
    search_urls = _build_search_urls(
        search_form_info, search_criteria, base_url, findings["homepage_nav"]
    )

    if search_criteria and search_urls:
        findings["search_attempted"] = True
        for search_url in search_urls[:5]:
            logger.info(
                "navigate_explore: STEP 3 (html) — trying search %s",
                search_url[:120],
            )
            search_html = fetch_fn(search_url)

            if not search_html or search_html.startswith("RENDER FAILED"):
                logger.warning(
                    "navigate_explore: search fetch failed for %s: %s",
                    search_url[:100],
                    (search_html[:100] if search_html else "(empty)"),
                )
                continue
            if "oops!" in search_html[:5000].lower():
                logger.info("navigate_explore: search %s returned 'oops!' page", search_url[:100])
                findings.setdefault("errors", []).append(
                    f"Search {search_url} returned 'oops!' page — session gating likely"
                )
                continue

            logger.info(
                "navigate_explore: search HTML len=%d for %s",
                len(search_html),
                search_url[:100],
            )

            try:
                search_soup = BeautifulSoup(search_html[:500000], "html.parser")
                product_links = _extract_product_links_bs(search_soup, base_url)
                findings["listing_page"]["product_links"] = product_links

                json_ld = _extract_json_ld(search_soup, base_url)
                if json_ld.get("products"):
                    findings["listing_page"]["json_ld"] = json_ld
                    existing = set(p.get("href", "") for p in product_links)
                    for p in json_ld["products"]:
                        if p.get("href") and p["href"] not in existing:
                            product_links.append(p)
                            existing.add(p["href"])
                    findings["listing_page"]["product_links"] = product_links
                    logger.info(
                        "navigate_explore: JSON-LD added %d product links from search",
                        len(json_ld["products"]),
                    )

                if _has_real_product_links(findings):
                    findings["listing_page"]["url"] = search_url
                    visited = True
                    break
            except Exception:
                pass

    # STEP 3b: If search failed, try categories matching search_criteria
    if not visited:
        best_cat = _pick_category_to_visit(category_links, search_criteria, base_url)
        if best_cat:
            candidate_urls = [best_cat]
        else:
            candidate_urls = [link["href"] for link in category_links[:5]]

        seen_cat_urls: set[str] = set()
        for cat_url in candidate_urls:
            if cat_url in seen_cat_urls:
                continue
            seen_cat_urls.add(cat_url)
            logger.info(
                "navigate_explore: STEP 3b — trying criteria-matched category %s",
                cat_url,
            )
            cat_html = fetch_fn(cat_url)
            if not cat_html or cat_html.startswith("RENDER FAILED"):
                logger.warning(
                    "navigate_explore: category fetch failed for %s",
                    cat_url,
                )
                continue
            if "oops!" in cat_html[:5000].lower():
                logger.info(
                    "navigate_explore: category %s returned 'oops!' page, skipping",
                    cat_url,
                )
                findings.setdefault("errors", []).append(
                    f"Category {cat_url} returned 'oops!' page — session gating likely"
                )
                continue
            logger.info(
                "navigate_explore: category HTML len=%d for %s",
                len(cat_html),
                cat_url,
            )
            try:
                cat_soup = BeautifulSoup(cat_html[:500000], "html.parser")
                cat_links = _extract_product_links_bs(cat_soup, base_url)
                cat_json_ld = _extract_json_ld(cat_soup, base_url)
                if cat_links or (cat_json_ld and cat_json_ld.get("products")):
                    findings["listing_page"]["url"] = cat_url
                    findings["listing_page"]["product_links"] = cat_links
                    if cat_json_ld.get("products"):
                        findings["listing_page"]["json_ld"] = cat_json_ld
                        existing = set(p.get("href", "") for p in cat_links)
                        for p in cat_json_ld["products"]:
                            if p.get("href") and p["href"] not in existing:
                                cat_links.append(p)
                                existing.add(p["href"])
                        findings["listing_page"]["product_links"] = cat_links
                        logger.info(
                            "navigate_explore: JSON-LD from category %s: %d products",
                            cat_url,
                            len(cat_json_ld["products"]),
                        )
                    if cat_json_ld.get("breadcrumbs"):
                        findings["homepage_nav"]["breadcrumbs"] = cat_json_ld[
                            "breadcrumbs"
                        ]
                    visited = True
                    break
            except Exception:
                pass

    if not visited:
        category_to_visit = _pick_category_to_visit(
            category_links, search_criteria, base_url
        )
        if category_to_visit:
            logger.info(
                "navigate_explore: STEP 3 (html) — visiting category %s",
                category_to_visit,
            )
            listing_html = fetch_fn(category_to_visit)
            findings["listing_page"]["url"] = category_to_visit

            try:
                listing_soup = BeautifulSoup(listing_html[:500000], "html.parser")
            except Exception:
                listing_soup = None

            if listing_soup:
                product_links = _extract_product_links_bs(listing_soup, base_url)
                findings["listing_page"]["product_links"] = product_links

                json_ld = _extract_json_ld(listing_soup, base_url)
                if json_ld.get("products"):
                    findings["listing_page"]["json_ld"] = json_ld
                    existing = set(p.get("href", "") for p in product_links)
                    for p in json_ld["products"]:
                        if p.get("href") and p["href"] not in existing:
                            product_links.append(p)
                            existing.add(p["href"])
                    findings["listing_page"]["product_links"] = product_links
                    logger.info(
                        "navigate_explore: JSON-LD added %d product links from category",
                        len(json_ld["products"]),
                    )

                if json_ld.get("breadcrumbs"):
                    findings["homepage_nav"]["breadcrumbs"] = json_ld["breadcrumbs"]

                # Pagination
                _extract_pagination_bs(listing_soup, base_url, findings["listing_page"])

                # Page count and total products
                page_count_input = listing_soup.select_one(
                    'input[name="page-count"], input[name="pageCount"]'
                )
                if page_count_input and page_count_input.get("value", "").isdigit():
                    findings["listing_page"]["page_count"] = int(
                        page_count_input["value"]
                    )

                total_el = listing_soup.select_one(
                    "#products_total, [id*='product-count'], .total-products"
                )
                if total_el:
                    match = re.search(r"\d+", total_el.get_text())
                    if match:
                        findings["listing_page"]["total_products"] = int(match.group())

                visited = True

    if not visited and not category_links and not search_form_info:
        findings["errors"].append(
            "No categories or search available for HTTP exploration"
        )

    # URL pattern detection
    all_links = category_links + findings.get("listing_page", {}).get(
        "product_links", []
    )
    url_patterns = _detect_url_patterns(all_links, base_url)
    if url_patterns:
        findings["url_patterns"] = url_patterns

    return findings


# ── Graph node entry point ─────────────────────────────────────────────────


def navigate_explore(state: dict, config=None) -> dict[str, Any]:
    """Deterministic navigation exploration graph node.

    Produces ``navigation_findings.json`` in workspace/{slug}/ with raw
    extracted data. The downstream ``navigation_synthesize`` node reads
    this and produces the structured ``navigation_analysis.json``.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    url = state.get("url", "")
    search_criteria = state.get("search_criteria", "")
    input_mode = state.get("input_mode", "navigation")
    search_url = (
        state.get("product_url", "") or state.get("search_url", "")
        if input_mode == "navigation"
        else ""
    )

    logger.info(
        "navigate_explore: starting (job %s, slug=%s, url=%s, mode=%s, "
        "search_criteria=%s, search_url=%s)",
        job_id,
        slug,
        url,
        input_mode,
        search_criteria[:50],
        search_url[:100] if search_url else "(none)",
    )

    root = getattr(settings, "PROJECT_ROOT", os.getcwd())
    site_analysis = _read_site_analysis(root, slug)

    # ── Route based on probe determination ──────────────────────────────
    playwright_unavailable = False

    # STRATEGY: Always try Playwright MCP first (it maintains session cookies
    # and can navigate interactive sites). Fall back to probe_html only
    # when Playwright MCP is truly unavailable. Even for UC Chrome sites,
    # Playwright MCP Chrome has often proven to work for navigation.
    use_playwright_first = True

    # Detect locale prefix from site_analysis product URL pattern
    effective_url = url
    prod_pattern = site_analysis.get("product_discovery", {}).get(
        "product_url_pattern", ""
    )
    locale_match = re.match(r"^(/[a-z]{2}-[a-z]{2,4}/)", prod_pattern)
    if locale_match:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        effective_url = origin + locale_match.group(1).rstrip("/")
        logger.info(
            "navigate_explore: detected locale %s from product URL pattern",
            locale_match.group(1),
        )

    # ── PHASE 1: Try Playwright MCP first (maintains session cookies) ────
    findings: dict[str, Any] = {}
    browser_ok = False
    try:
        from agents.tools.playwright_tools import create_playwright_tools_sync

        pw_tools = create_playwright_tools_sync(fresh=True)
        if pw_tools:
            browser_ok = True
            from agents.tools.context import set_tool_context

            nav_state = dict(state)
            nav_state["probe_result"] = {}
            set_tool_context(nav_state, agent_name="navigation_explore")
            try:
                explore_url = effective_url if effective_url != url else url
                findings = _do_explore_via_browser(
                    pw_tools,
                    explore_url,
                    search_criteria,
                    site_analysis,
                    search_url=search_url,
                )
            finally:
                from agents.tools.context import clear_tool_context

                clear_tool_context()
        else:
            playwright_unavailable = True
            logger.warning(
                "navigate_explore: Playwright MCP unavailable, falling back to HTTP"
            )
    except Exception as exc:
        logger.exception("navigate_explore: browser exploration failed: %s", exc)

    # Retry browser once after clearing cache
    if not browser_ok or not findings.get("homepage_nav", {}).get("category_links"):
        if not browser_ok:
            try:
                import agents.tools.playwright_tools as _pw

                _pw._cached_tools = None  # type: ignore[attr-defined]
                pw_tools = create_playwright_tools_sync(fresh=True)
                if pw_tools:
                    browser_ok = True
                    playwright_unavailable = False
                    from agents.tools.context import set_tool_context

                    set_tool_context(dict(state), agent_name="navigation_explore")
                    try:
                        findings = _do_explore_via_browser(
                            pw_tools,
                            url,
                            search_criteria,
                            site_analysis,
                            search_url=search_url,
                        )
                    finally:
                        from agents.tools.context import clear_tool_context

                        clear_tool_context()
            except Exception as exc:
                logger.warning("navigate_explore: browser retry failed: %s", exc)

    # ── PHASE 2: probe_html fallback for search page when Playwright finds
    #    categories but 0 products (Cloudflare blocks JS rendering).  We keep
    #    the browser homepage_nav but replace listing_page with UC Chrome data.
    browser_cats = len(findings.get("homepage_nav", {}).get("category_links", []))
    browser_prods = len(findings.get("listing_page", {}).get("product_links", []))
    if search_criteria and browser_cats > 0 and browser_prods == 0:
        if _needs_uc_chrome(site_analysis):
            logger.info(
                "navigate_explore: Playwright found %d categories but 0 search products "
                "on UC Chrome site — fetching search page via probe_html",
                browser_cats,
            )
            search_url = _build_search_url(
                findings.get("homepage_nav", {}).get("search_form"),
                search_criteria,
                effective_url,
                findings.get("homepage_nav", {}),
            )
            if search_url:
                html = _fetch_via_probe_html(search_url)
                if html and "RENDER FAILED" not in html:
                    try:
                        from bs4 import BeautifulSoup

                        search_soup = BeautifulSoup(html[:500000], "html.parser")
                        product_links = _extract_product_links_bs(
                            search_soup, effective_url
                        )
                        json_ld = _extract_json_ld(search_soup, effective_url)
                        existing = set(p.get("href", "") for p in product_links)
                        if json_ld.get("products"):
                            for p in json_ld["products"]:
                                if p.get("href") and p["href"] not in existing:
                                    product_links.append(p)
                                    existing.add(p["href"])
                        if product_links:
                            listing = findings.setdefault("listing_page", {})
                            listing["product_links"] = product_links
                            listing["url"] = search_url
                            listing["json_ld"] = json_ld
                            browser_prods = len(product_links)
                            logger.info(
                                "navigate_explore: probe_html found %d product links "
                                "on search page, merged into browser findings",
                                browser_prods,
                            )
                    except Exception as exc:
                        logger.warning(
                            "navigate_explore: probe_html search parse failed: %s", exc
                        )

    # ── PHASE 3: Fall back to probe_html or HTTP if entirely empty ───
    if not findings or not findings.get("homepage_nav", {}).get("category_links"):
        if _needs_uc_chrome(site_analysis):
            logger.info(
                "navigate_explore: Playwright failed for UC Chrome site, trying probe_html"
            )
            http_findings = _do_explore_via_http(
                effective_url,
                search_criteria,
                site_analysis,
                fetch_fn=_fetch_via_probe_html,
            )
        else:
            http_findings = _do_explore_via_http(url, search_criteria, site_analysis)

        http_cats = len(
            http_findings.get("homepage_nav", {}).get("category_links", [])
        )
        http_prods = len(
            http_findings.get("listing_page", {}).get("product_links", [])
        )
        browser_cats = len(
            findings.get("homepage_nav", {}).get("category_links", [])
        )
        browser_prods = len(
            findings.get("listing_page", {}).get("product_links", [])
        )
        http_is_better = (
            (http_cats > browser_cats and http_cats > 0)
            or (http_prods > browser_prods and http_prods > 0)
        )
        if http_is_better:
            logger.info(
                "navigate_explore: HTTP found %d cats/%d prods vs browser %d cats/%d prods — using HTTP",
                http_cats, http_prods, browser_cats, browser_prods,
            )
            findings = http_findings
        else:
            http_cat_links = http_findings.get("homepage_nav", {}).get(
                "category_links", []
            )
            if http_cat_links:
                findings.setdefault("homepage_nav", {}).setdefault(
                    "category_links", []
                ).extend(http_cat_links)
                logger.info(
                    "navigate_explore: merged %d HTTP categories into browser findings",
                    len(http_cat_links),
                )

    # Write findings to workspace
    findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
    os.makedirs(os.path.dirname(findings_path), exist_ok=True)

    findings["metadata"] = {
        "site_url": url,
        "site_slug": slug,
        "search_criteria": search_criteria,
        "input_mode": input_mode,
        "exploration_method": findings.get("method", "unknown"),
        "site_analysis_method": site_analysis.get("connectivity", {}).get(
            "method_that_worked", "unknown"
        ),
    }

    with open(findings_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2, ensure_ascii=False)

    logger.info(
        "navigate_explore: completed (job %s) — wrote %s "
        "(categories=%d, product_links=%d, errors=%d)",
        job_id,
        findings_path,
        len(findings.get("homepage_nav", {}).get("category_links", [])),
        len(findings.get("listing_page", {}).get("product_links", [])),
        len(findings.get("errors", [])),
    )

    _persist_explore_summary(job_id, findings)

    result: dict[str, Any] = {
        "navigation_findings": findings,
    }

    if playwright_unavailable and not _needs_uc_chrome(site_analysis):
        result["playwright_unavailable"] = True
        logger.warning(
            "navigate_explore: Playwright MCP unavailable for non-Akamai site — "
            "flagging for user interrupt"
        )

    return result
