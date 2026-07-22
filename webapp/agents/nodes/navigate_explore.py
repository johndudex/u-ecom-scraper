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
    api_endpoints: [],
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
    // React data-testid patterns (PVH/CK, Next.js commerce)
    '[data-testid*="GridItem"]',
    '[data-testid*="product" i]',
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
        const isSelfLink = card.tagName === 'A' && card.href && !link;
        const targetLink = isSelfLink ? card : link;
        if (!targetLink) return;
        const href = targetLink.href;
        if (!href || seen.has(href) || href.startsWith('#')) return;
        seen.add(href);
        const text = (targetLink.getAttribute('data-productname') ||
                     targetLink.getAttribute('title') ||
                     targetLink.getAttribute('aria-label') ||
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
          const val = targetLink.getAttribute(attr) || card.getAttribute(attr);
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

  // --- Range-with-total pattern (e.g. locumtenens "1 - 100 of 3771") -----
  // The signal is the TEXT pattern (\d+ - \d+ of \d+), NOT a class glob —
  // sites use unpredictable classes for the count element (locumtenens uses
  // `.cds-text-midnight.cds-text-fw-bold`, which the allowlist above misses).
  // Walk visible leaf text elements and capture the RAW count string into
  // item_count_text so navigate_synthesize's parse_count_string() can derive
  // total_items and the orchestrator can stamp source="site_reported".
  // Currency/decimal strings are rejected downstream by the parser.
  if (!result.item_count_text) {
    const rangeRe = /(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d{1,3}(?:,\d{3})+|\d+)/i;
    const leaves = document.querySelectorAll(
      'div, span, p, h1, h2, h3, h4, h5, label, li, small, em, strong'
    );
    for (let i = 0; i < leaves.length && i < 4000; i++) {
      const el = leaves[i];
      // Skip containers whose descendants also match — we want the deepest
      // element holding the count text (avoids duplicating parent totals).
      if (el.querySelector('div, span, p, li, h1, h2, h3, h4, label')) continue;
      const txt = (el.innerText || el.textContent || '').trim();
      if (!txt || txt.length > 80) continue;
      const m = txt.match(rangeRe);
      if (m) {
        result.item_count_text = txt;
        if (!result.total_products) {
          const total = parseInt(m[3].replace(/,/g, ''), 10);
          if (total) result.total_products = total;
        }
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

  // --- Detect filter parameters (URL-based + form-based) ---
  // Job portals and search sites use filters for date (posted age),
  // location, and category/job-type. Capture both URL params (URL filtering)
  // and form elements (form-based filtering) so the generated scraper can
  // apply the right mechanism per site.
  const detectedFilters = {
    url_date_params: [],
    url_location_params: [],
    url_category_params: [],
    url_other_params: []
  };
  try {
    const urlParams = new URLSearchParams(window.location.search);
    for (const [key, value] of urlParams.entries()) {
      const k = key.toLowerCase();
      if (['date_posted','posted','posteddate','days','fromage','daterange','date','pd','postedwithin','age'].includes(k)) {
        detectedFilters.url_date_params.push({param: key, value: value});
      } else if (['location','l','loc','city','state','st','radius','lat','lng','geo','where','region'].includes(k)) {
        detectedFilters.url_location_params.push({param: key, value: value});
      } else if (['category','cat','job_type','jt','specialty','discipline','profession','department','profession_id'].includes(k)) {
        detectedFilters.url_category_params.push({param: key, value: value});
      } else {
        detectedFilters.url_other_params.push({param: key, value: value});
      }
    }
  } catch(e) {}
  result.detected_filters = detectedFilters;

  // Detect filter UI elements (dropdowns / inputs for date, location, category)
  const filterUI = {
    date_selectors: [],
    location_selectors: [],
    category_selectors: []
  };
  function buildElSelector(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    if (el.className && typeof el.className === 'string') {
      const firstClass = el.className.trim().split(/\s+/)[0];
      if (firstClass) return el.tagName.toLowerCase() + '.' + firstClass;
    }
    return null;
  }
  function describeEl(el) {
    const entry = {};
    const sel = buildElSelector(el);
    if (!sel) return null;
    entry.selector = sel;
    if (el.name) entry.name = el.name;
    if (el.id) entry.id = el.id;
    if (el.tagName === 'SELECT') {
      entry.options = Array.from(el.options).slice(0, 20).map(function(o) { return o.value || o.text; });
    } else if (el.placeholder) {
      entry.placeholder = el.placeholder;
    }
    return entry;
  }
  const filterEls = document.querySelectorAll(
    'select, input[type="text"], input[type="search"], input[type="date"], input:not([type])'
  );
  filterEls.forEach(function(el) {
    const name = (el.name || el.id || '').toLowerCase();
    const placeholder = (el.placeholder || '').toLowerCase();
    const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
    const combined = name + ' ' + placeholder + ' ' + ariaLabel;
    if (!combined || combined.trim() === '') return;
    if (/\b(date|posted|days|fromage|recent|age)\b/.test(combined)) {
      const e = describeEl(el);
      if (e) filterUI.date_selectors.push(e);
    } else if (/\b(location|state|city|zip|postal|radius|where|region|geo)\b/.test(combined)) {
      const e = describeEl(el);
      if (e) filterUI.location_selectors.push(e);
    } else if (/\b(category|specialty|discipline|profession|job.?type|department|profession)\b/.test(combined)) {
      const e = describeEl(el);
      if (e) filterUI.category_selectors.push(e);
    }
  });
  result.filter_ui = filterUI;

  // --- Detect backend JSON API endpoints (React/Vue SPAs over XHR) ---
  // Many modern boards (e.g. AMN Healthcare) render listings client-side by
  // fetching JSON from a backend search API.  Capture those XHR/fetch resource
  // URLs so the code-writer can emit a clean HTTP api_scraper instead of a
  // fragile browser driver.  Reads the browser's Performance Resource Timing
  // entries (no response bodies, but the URL + query is enough to reproduce).
  try {
    const apiRE = /(job|search|listing|feed|result|position|vacanc|posting)/i;
    const seen = new Set();
    const candidates = [];
    const entries = (performance && performance.getEntriesByType)
      ? performance.getEntriesByType('resource') : [];
    for (const e of entries) {
      const url = e.name || '';
      if (!url || seen.has(url)) continue;
      // initiator fetch/xhr, or any URL that looks like a JSON search API
      const isXhr = (e.initiatorType === 'xmlhttprequest' || e.initiatorType === 'fetch');
      const looksApi = apiRE.test(url) || /\/api\/|\/v1\/|search/i.test(url);
      if (!(isXhr || looksApi)) continue;
      seen.add(url);
      // Do NOT truncate — a query-heavy search URL (e.g. AMN's /JobSearch with
      // ~16 repeated FilterTypes) can exceed 400 chars; slicing there corrupts
      // the LAST param value (PayRateType -> PayRateTyp), which the API then
      // rejects with HTTP 400.  Keep the full URL.
      candidates.push({ url: url, method: 'GET', initiator: e.initiatorType || '' });
    }
    // Heuristic ranking: prefer URLs that mention job/search and have query params
    // (those are the real listing/search calls, not telemetry).
    candidates.sort((a, b) => {
      const score = (c) => {
        let s = 0;
        if (/job/i.test(c.url)) s += 3;
        if (/search/i.test(c.url)) s += 2;
        if (c.url.includes('?')) s += 2;
        if (c.url.includes('PageNumber') || c.url.includes('page=')) s += 2;
        if (/\/api\/|\/v1\//i.test(c.url)) s += 1;
        return s;
      };
      return score(b) - score(a);
    });
    result.api_endpoints = candidates.slice(0, 8);
  } catch (apiErr) {
    result.api_endpoints = [];
  }

  // --- Detect EMBEDDED item-JSON (generic data-model signal) ----------------
  // Many modern sites embed the full item dataset as a JSON array inside a
  // <script> tag in the listing/category HTML (e.g. window.jobsData=[...],
  // Next.js #__NEXT_DATA__, Nuxt #__NUXT__, JSON-LD ItemList). This is a THIRD
  // data model — distinct from "detail pages" — where the listing page itself
  // contains every record. Detecting the largest homogeneous array of
  // record-like objects lets navigation pick the data-richest listing and lets
  // code_writer extract from the listing JSON instead of scraping detail pages.
  // CONTENT-AGNOSTIC: keys on "array of record objects", so it works for
  // products, jobs, articles, events — anything. No site-specific names.
  try {
    // defaults so a partial failure still leaves a sane signal
    result.embedded_json = {detected: false, sources: [], best: null};
    result.data_richness = (result.product_links || []).length;
    result.data_source = (result.product_links || []).length >= 3 ? 'detail_links' : 'none';

    // A "record array" = >=3 objects, each with >=3 primitive fields, sharing
    // >=2 keys (homogeneous). Returns {count, keys} or null.
    function isRecordArray(arr) {
      if (!Array.isArray(arr) || arr.length < 3) return null;
      const objs = arr.filter(x => x && typeof x === 'object' && !Array.isArray(x));
      if (objs.length < 3) return null;
      const primKeySets = [];
      for (const o of objs) {
        const ks = Object.keys(o).filter(k => {
          const v = o[k];
          return v === null || typeof v !== 'object';
        });
        if (ks.length < 3) return null;
        primKeySets.push(ks);
      }
      const counts = {};
      primKeySets.forEach(ks => ks.forEach(k => counts[k] = (counts[k] || 0) + 1));
      const shared = Object.keys(counts).filter(k => counts[k] >= objs.length * 0.6);
      if (shared.length < 2) return null;
      const topKeys = Object.keys(counts).sort((a, b) => counts[b] - counts[a]).slice(0, 20);
      return {count: arr.length, keys: topKeys};
    }
    function sanitizeRecord(o) {
      if (!o || typeof o !== 'object') return o;
      const out = {};
      let n = 0;
      for (const k of Object.keys(o)) {
        if (n >= 25) break;
        let v = o[k];
        if (Array.isArray(v)) v = '[...]';
        else if (v && typeof v === 'object') v = '{...}';
        else if (typeof v === 'string' && v.length > 140) v = v.slice(0, 140) + '…';
        out[k] = v;
        n++;
      }
      return out;
    }
    // Deep-walk a parsed JSON value for its largest record array; remember the path.
    // Classify a record array by its locator/path's last segment so structural
    // taxonomy arrays (categories/specialties/professions/filters) don't outrank
    // the real item array (jobs/products). Mirrors _classify_array_name in Python.
    const ITEM_NOUNS = ['job','product','item','result','listing','post','article','record','vacanc','opening','feed','inventory','catalog','stock','sku','card','entry','course','event','property','vehicle','recipe','deal','order'];
    const TAX_NOUNS = ['categor','special','profession','disciplin','expertis','department','facet','filter','refin','taxonom','tag','skill','qualif','certif','breadcrumb','menu','subtyp','locale','geograph','region','countr','zone','borough','spec'];
    function classifyArray(path) {
      if (!path) return 'neutral';
      let seg = path.replace(/\[[^\]]*\]/g, '').split('.').pop().replace(/^#/, '').toLowerCase();
      if (TAX_NOUNS.some(t => seg.indexOf(t) !== -1)) return 'taxonomy';
      if (ITEM_NOUNS.some(t => seg.indexOf(t) !== -1)) return 'item';
      return 'neutral';
    }
    function walk(val, path, depth, best) {
      if (depth > 6 || val == null) return;
      if (Array.isArray(val)) {
        const info = isRecordArray(val);
        if (info) {
          const cls = classifyArray(path);
          const score = cls === 'taxonomy' ? -1 : (cls === 'item' ? info.count * 3 : info.count);
          if (score > best.score) {
            best.score = score;
            best.count = info.count;
            best.keys = info.keys;
            best.path = path;
            best.record = sanitizeRecord(val.find(x => x && typeof x === 'object' && !Array.isArray(x)));
          }
        }
        for (let i = 0; i < Math.min(val.length, 15); i++) walk(val[i], path + '[' + i + ']', depth + 1, best);
      } else if (typeof val === 'object') {
        for (const k of Object.keys(val)) walk(val[k], path ? path + '.' + k : k, depth + 1, best);
      }
    }
    // Balanced-bracket slice: text[openIdx]==='[' -> substring incl. matching ']'.
    function balancedArray(text, openIdx) {
      let depth = 0, inStr = false, q = '';
      for (let i = openIdx; i < text.length; i++) {
        const c = text[i];
        if (inStr) {
          if (c === '\\') { i++; continue; }
          if (c === q) inStr = false;
          continue;
        }
        if (c === '"' || c === "'") { inStr = true; q = c; continue; }
        if (c === '[') depth++;
        else if (c === ']') { depth--; if (depth === 0) return text.slice(openIdx, i + 1); }
      }
      return null;
    }
    const sources = [];
    function addSource(kind, locator, best, preview) {
      if (!best || best.count < 3) return;
      sources.push({
        kind: kind,
        locator: locator,
        record_count: best.count,
        array_path: best.path || '',
        sample_keys: best.keys || [],
        sample_record: best.record || null,
        script_preview: (preview || '').slice(0, 300),
      });
    }
    function freshBest() { return {count: 0, score: -1, keys: [], path: '', record: null}; }

    // 1) JSON-LD blocks (ItemList / arrays of typed records)
    document.querySelectorAll('script[type="application/ld+json"]').forEach((s, idx) => {
      const raw = (s.textContent || '').trim();
      if (!raw) return;
      let data;
      try { data = JSON.parse(raw); } catch (e) { return; }
      const items = Array.isArray(data) ? data : [data];
      const best = freshBest();
      items.forEach(it => walk(it, 'jsonld.' + ((it && typeof it === 'object' && it['@type']) || 'block'), 0, best));
      if (best.count >= 3) {
        const typed = items.find(x => x && typeof x === 'object' && x['@type']);
        const t = (typed && typed['@type']) || ('block' + idx);
        addSource('jsonld', 'jsonld:' + t, best, raw);
      }
    });

    // 2) Next.js / Nuxt hydration data
    [['#__NEXT_DATA__', 'next_data'], ['#__NUXT__', 'nuxt_data']].forEach(pair => {
      const sel = pair[0], kind = pair[1];
      const el = document.querySelector(sel);
      if (!el) return;
      try {
        const data = JSON.parse(el.textContent || '');
        const best = freshBest();
        walk(data, sel, 0, best);
        if (best.count >= 3) addSource(kind, sel, best, (el.textContent || '').slice(0, 300));
      } catch (e) {}
    });

    // 3) Inline <script> JSON blobs: whole-JSON scripts + named assignments.
    // Bounded (<=30 scripts, <=1MB each, <=20 assignments each) so a giant
    // webpack bundle never stalls extraction.
    const scripts = Array.from(document.querySelectorAll('script:not([src])')).slice(0, 30);
    scripts.forEach(s => {
      let txt = (s.textContent || '').trim();
      if (!txt || txt.length < 30) return;
      if (txt.length > 1000000) txt = txt.slice(0, 1000000);
      // whole-script JSON?
      if (txt[0] === '[' || txt[0] === '{') {
        try {
          const best = freshBest();
          walk(JSON.parse(txt), 'script', 0, best);
          if (best.count >= 3) { addSource('inline_script', 'script-json', best, txt.slice(0, 300)); return; }
        } catch (e) {}
      }
      // named assignments: NAME = [ { ... } ]  (window.foo.bar, var x, obj key)
      const re = /(?:^|[;\n\s{}(,])((?:[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){0,4}))\s*[:=]\s*\[\s*\{/g;
      let m, attempts = 0;
      while ((m = re.exec(txt)) !== null && attempts < 20) {
        attempts++;
        const name = m[1];
        const openIdx = m.index + m[0].lastIndexOf('[');
        const sub = balancedArray(txt, openIdx);
        if (!sub || sub.length > 8000000) continue;
        try {
          const best = freshBest();
          walk(JSON.parse(sub), name, 0, best);
          if (best.count >= 3) { addSource('inline_script', name, best, txt.slice(openIdx, openIdx + 300)); return; }
        } catch (e) {}
      }
    });

    // Aggregate: richest source wins.
    sources.sort((a, b) => b.record_count - a.record_count);
    const bestSrc = sources[0] || null;
    const embCount = bestSrc ? bestSrc.record_count : 0;
    const linkCount = (result.product_links || []).length;
    result.embedded_json = {detected: !!bestSrc, sources: sources.slice(0, 5), best: bestSrc};
    result.data_richness = Math.max(embCount, linkCount);
    if (embCount >= 3) result.data_source = 'embedded_json';
    else if (linkCount >= 3) result.data_source = 'detail_links';
    else result.data_source = 'none';
  } catch (embErr) {
    // Never let detection break the rest of the extraction result.
    result.embedded_json = result.embedded_json || {detected: false, sources: [], best: null};
  }

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

        cats = (findings.get("homepage_nav") or {}).get("category_links", [])
        prods = (findings.get("listing_page") or {}).get("product_links", [])
        errors = findings.get("errors") or []
        search_form = (findings.get("homepage_nav") or {}).get("search_form")
        url_patterns = findings.get("url_patterns") or {}
        pagination = (findings.get("listing_page") or {}).get("pagination") or {}

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

        common_patterns = [
            f"{origin}/search?q={criteria_encoded}",
            f"{origin}{base_path}/search.aspx?search={criteria_encoded}",
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
    '[data-testid*="GridItem"]',
    '[data-testid*="product" i]',
    '[data-testid*="PriceDisplay"]',
    '[data-testid*="PriceText"]',
]

# Regexes (applied to absolute hrefs) that identify a JOB-DETAIL link on job
# boards.  Unconditional/safe: e-commerce sites have none of these, so adding
# them to content detection cannot cause false positives on product sites — it
# only lets _wait_for_content recognize job boards (e.g. AMN's
# /job-details/{id}/{slug}/) as "content present" instead of timing out and
# discarding the real job links as stale DOM.
_JOB_LINK_HREF_PATTERNS = [
    r"/job-details/",
    r"/job-details\.",
    r"/jobs/job[-/]?",
    r"/job-posting",
    r"/jobposting",
    r"/careers/job/",
    r"/jobs/view/",
    r"/position/[a-z0-9-]*\d{4,}",
    # /job(s)/<slug-with-id>/  (id >= 4 digits anywhere in a jobs path segment)
    r"/jobs?/[a-z0-9_-]*\d{4,}[a-z0-9_-]*/?",
]


# ── Embedded-JSON detection in raw HTML (Python twin of the in-DOM detector) ─
# Used by _verify_rendering to decide whether the listing's embedded item data is
# reachable via a plain HTTP fetch (SSR) or only after JS rendering (CSR). This
# is the right signal for "data in a <script> JSON blob" sites — raw-vs-rendered
# *link* counts misread them (a page can have ~10 cards but a 500-record blob).

def _is_record_list(arr) -> int | None:
    """Return the count if ``arr`` is a homogeneous array of record-like dicts.

    A record array: >=3 dicts, each with >=3 primitive (non-container) fields,
    sharing >=2 keys across >=60% of records. Content-agnostic. Mirrors the
    in-browser ``isRecordArray`` in ``_LISTING_PAGE_EXTRACTION_JS``.
    """
    if not isinstance(arr, list) or len(arr) < 3:
        return None
    objs = [x for x in arr if isinstance(x, dict)]
    if len(objs) < 3:
        return None
    counts: dict[str, int] = {}
    for o in objs:
        ks = [k for k, v in o.items() if not isinstance(v, (dict, list))]
        if len(ks) < 3:
            return None
        for k in set(ks):
            counts[k] = counts.get(k, 0) + 1
    shared = [1 for k, c in counts.items() if c >= len(objs) * 0.6]
    if len(shared) < 2:
        return None
    return len(arr)


# ── Array-name classification (item vs taxonomy) ────────────────────────────
# A listing page often embeds BOTH the item dataset (e.g. jobsData) AND larger
# structural/taxonomy arrays (specialties, professions, categories, filters).
# The "largest record array" is frequently the taxonomy, NOT the items — picking
# it would make code_writer extract specialties instead of jobs (ayahealthcare
# case: expertises=110 vs jobsData=10). Classify by the array's locator name:
# exclude taxonomy/facet arrays, prefer item arrays. Generic site-structure
# vocabulary — NOT site-specific. Mirrored in the JS detector.
_ITEM_NOUNS = (
    "job", "product", "item", "result", "listing", "post", "article", "record",
    "vacanc", "opening", "feed", "inventory", "catalog", "stock", "sku", "card",
    "entry", "course", "event", "property", "vehicle", "recipe", "deal", "order",
)
_TAXONOMY_NOUNS = (
    "categor", "special", "profession", "disciplin", "expertis", "department",
    "facet", "filter", "refin", "taxonom", "tag", "skill", "qualif", "certif",
    "breadcrumb", "menu", "subtyp", "locale", "geograph", "region", "countr",
    "zone", "borough", "spec",  # speciality/spec
)


def _classify_array_name(path: str) -> str:
    """Classify a record array by its locator/path's last segment.

    Returns ``'taxonomy'`` (structural: categories/specialties/professions/
    filters — NOT the items), ``'item'`` (jobs/products/listings), or
    ``'neutral'``. Generic site-structure vocabulary; no site-specific names.
    """
    if not path:
        return "neutral"
    seg = re.sub(r"\[[^\]]*\]", "", str(path)).split(".")[-1].lower().lstrip("#")
    if any(t in seg for t in _TAXONOMY_NOUNS):
        return "taxonomy"
    if any(t in seg for t in _ITEM_NOUNS):
        return "item"
    return "neutral"


def _record_top_keys(arr) -> list:
    """Top field names by frequency across the record dicts in ``arr``."""
    counts: dict[str, int] = {}
    for x in arr:
        if isinstance(x, dict):
            for k, v in x.items():
                if not isinstance(v, (dict, list)):
                    counts[k] = counts.get(k, 0) + 1
    return sorted(counts, key=lambda k: -counts[k])[:20]


def _sanitize_record(arr) -> dict | None:
    """A truncated, JSON-safe copy of the first record dict in ``arr``."""
    for x in arr:
        if isinstance(x, dict):
            out: dict = {}
            for i, (k, v) in enumerate(x.items()):
                if i >= 25:
                    break
                if isinstance(v, list):
                    v = "[...]"
                elif isinstance(v, dict):
                    v = "{...}"
                elif isinstance(v, str) and len(v) > 140:
                    v = v[:140] + "…"
                out[k] = v
            return out
    return None


def _find_best_record_array(obj, best: dict, path: str = "", depth: int = 0) -> None:
    """Deep-walk a parsed JSON value; set ``best`` to the best ITEM record array.

    Scoring excludes taxonomy/facet arrays (so a 110-element `expertises`
    taxonomy never beats a 10-element `jobsData`) and boosts item-named arrays.
    ``best`` keys: count, score, path, keys, record.
    """
    if depth > 6 or obj is None:
        return
    if isinstance(obj, list):
        n = _is_record_list(obj)
        if n:
            cls = _classify_array_name(path)
            score = -1 if cls == "taxonomy" else (n * 3 if cls == "item" else n)
            if score > best.get("score", -1):
                best.update({
                    "count": n, "score": score, "path": path,
                    "keys": _record_top_keys(obj), "record": _sanitize_record(obj),
                })
        for i, x in enumerate(obj[:20]):
            _find_best_record_array(x, best, f"{path}[{i}]", depth + 1)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _find_best_record_array(v, best, f"{path}.{k}" if path else str(k), depth + 1)


def _balanced_substr(text: str, open_idx: int, open_ch: str = "[", close_ch: str = "]") -> str | None:
    """Return text[open_idx..matching close] (inclusive), honoring string literals."""
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != open_ch:
        return None
    depth = 0
    in_str = False
    quote = ""
    i = open_idx
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
        i += 1
    return None


def _raw_html_has_embedded_json(html: str, locator_hints: list[str] | None = None) -> bool:
    """True if ``html`` embeds a homogeneous record-array in a <script> tag.

    Mirrors the in-DOM detector so the SSR-vs-CSR call is consistent. Scans
    JSON-LD, ``#__NEXT_DATA__``/``#__NUXT__``, and inline scripts (named
    assignments + whole-JSON). When ``locator_hints`` are given (tokens from the
    rendered-DOM detector, e.g. a variable name), scripts are filtered to those
    mentioning a hint for speed + precision, falling back to a full scan.
    Bounded; never raises.
    """
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return False
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    best: dict = {"count": 0, "score": -1}

    def _scan_json_text(txt: str) -> None:
        if not txt or len(txt) < 30:
            return
        if len(txt) > 1_000_000:
            txt = txt[:1_000_000]
        stripped = txt.strip()
        if stripped[:1] in "[{":
            try:
                _find_best_record_array(json.loads(stripped), best, path="script")
                if best["count"] >= 3:
                    return
            except (json.JSONDecodeError, ValueError):
                pass
        for m in re.finditer(
            r"(?:^|[;\n\s{}(,])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){0,4})\s*[:=]\s*\[\s*\{",
            txt,
        ):
            name = m.group(1)
            open_idx = txt.find("[", m.start(1))
            sub = _balanced_substr(txt, open_idx)
            if not sub or len(sub) > 8_000_000:
                continue
            try:
                _find_best_record_array(json.loads(sub), best, path=name)
            except (json.JSONDecodeError, ValueError):
                continue
            if best["count"] >= 3:
                return

    hints = [h for h in (locator_hints or []) if h]

    # JSON-LD (typed records are items by nature; classify by @type)
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            t = (it.get("@type") if isinstance(it, dict) else "") or "Block"
            _find_best_record_array(it, best, path=f"jsonld.{t}")

    # Next.js / Nuxt hydration (nested arrays inherit their key as the name)
    for sid in ("__NEXT_DATA__", "__NUXT__"):
        el = soup.find(id=sid)
        if el and el.string:
            try:
                _find_best_record_array(json.loads(el.string), best, path=sid)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    # Inline scripts (bounded; hint-filtered when hints available)
    for idx, s in enumerate(soup.find_all("script", src=False)):
        if idx > 40:
            break
        txt = s.string or s.get_text() or ""
        if not txt:
            continue
        if hints and not any(h in txt for h in hints):
            continue
        _scan_json_text(txt)
        if best["count"] >= 3:
            return True

    # If hints filtered everything out, retry the inline scripts without the filter.
    if hints and best["count"] < 3:
        for idx, s in enumerate(soup.find_all("script", src=False)):
            if idx > 40:
                break
            _scan_json_text(s.string or s.get_text() or "")
            if best["count"] >= 3:
                return True

    return best["count"] >= 3


def _embedded_json_locator_hints(listing: dict) -> list[str]:
    """Tokens from the rendered-DOM detector to guide the raw-HTML scan."""
    emb = listing.get("embedded_json") or {}
    if not isinstance(emb, dict):
        return []
    hints: list[str] = []
    for src in (emb.get("sources") or [])[:5]:
        if not isinstance(src, dict):
            continue
        loc = src.get("locator") or ""
        if loc:
            hints.append(loc)
            # also the last segment (variable name) for inline assignments
            if "." in loc:
                hints.append(loc.split(".")[-1])
    return hints


def _verify_rendering(findings: dict) -> None:
    """Determine whether the listing page's item links require JS rendering.

    Compares the rendered item-link count (from the Playwright extraction, in
    ``findings["listing_page"]["product_links"]``) against a raw-HTTP fetch of the
    same listing URL. If the raw HTML has 0 item links but the rendered DOM had
    some, the listings are JS-rendered (CSR) → ``_derive_strategy`` picks a browser
    strategy upfront instead of http_requests (the ayahealthcare failure: homepage
    reachable via direct_http but listings JS-rendered). Realizes the documented
    ``rendering_verified`` contract (SKILL.md). Bounded: one fetch, short timeout,
    errors → "unknown" (never blocks).
    """
    listing = findings.get("listing_page") or {}
    listing_url = (listing.get("url") or "").strip()
    rendered_count = len(listing.get("product_links") or [])
    listing["rendered_item_link_count"] = rendered_count
    # Embedded-JSON signal: a listing can have ~0 visible links yet carry the
    # whole dataset in a <script> blob. Don't bail on the raw-HTML check just
    # because there were no rendered links — the data may still be SSR-reachable.
    emb = listing.get("embedded_json") or {}
    emb_best = (emb.get("best") or {}) if isinstance(emb, dict) else {}
    emb_count = int(emb_best.get("record_count") or 0) if isinstance(emb_best, dict) else 0
    if not listing_url or (rendered_count == 0 and emb_count == 0):
        listing["rendering_verified"] = "unknown"
        listing["raw_html_product_link_count"] = 0
        return
    try:
        import httpx
        from bs4 import BeautifulSoup

        resp = httpx.get(
            listing_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15.0,
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            listing["rendering_verified"] = "unknown"
            listing["raw_html_product_link_count"] = 0
            return
        soup = BeautifulSoup(resp.text, "html.parser")
        raw_count = 0
        for sel in _PRODUCT_PRESENCE_SELECTORS:
            raw_count += len(soup.select(sel))
        job_pats = [re.compile(p) for p in _JOB_LINK_HREF_PATTERNS]
        for a in soup.find_all("a", href=True):
            if any(p.search(a["href"]) for p in job_pats):
                raw_count += 1
        listing["raw_html_product_link_count"] = raw_count
        if raw_count == 0 and rendered_count > 0:
            listing["rendering_verified"] = "csr"
        elif raw_count > 0:
            listing["rendering_verified"] = "ssr"
        else:
            listing["rendering_verified"] = "unknown"
        # Embedded-JSON override: for "data in a <script> blob" sites the link
        # count is the wrong signal. If the rendered DOM detected a record array,
        # check whether that array is ALSO in the raw HTML — if so the data is
        # SSR-reachable (http_requests works); if it's only in the rendered DOM
        # the page is CSR (needs a browser). This drives _derive_strategy's
        # existing ssr/cs/http_navigation cascade with no special-case branch.
        if emb_count >= 3:
            hints = _embedded_json_locator_hints(listing)
            if _raw_html_has_embedded_json(resp.text, hints):
                listing["rendering_verified"] = "ssr"
                listing["embedded_json_reachable_via"] = "raw_html"
            else:
                listing["rendering_verified"] = "csr"
                listing["embedded_json_reachable_via"] = "rendered_dom"
        logger.info(
            "navigate_explore: rendering_verified=%s (raw=%d rendered=%d emb=%d) for %s",
            listing["rendering_verified"], raw_count, rendered_count, emb_count, listing_url[:80],
        )
    except Exception as exc:
        logger.warning("navigate_explore: raw-HTML rendering check failed: %s", exc)
        listing["rendering_verified"] = "unknown"
        listing["raw_html_product_link_count"] = 0


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
            // Job-board fallback: >= 3 links whose href looks like a job detail
            // (e.g. AMN /job-details/3515728/...).  Lets us recognize React/SPA
            // job boards as "content present" so their links aren't discarded.
            const jobPats = {json.dumps(_JOB_LINK_HREF_PATTERNS)}.map(p => new RegExp(p));
            let jobCount = 0;
            for (const a of document.querySelectorAll('a[href]')) {{
                if (jobPats.some(re => re.test(a.href))) {{
                    jobCount++;
                    if (jobCount >= 3) {{
                        return JSON.stringify({{present: true, selector: "job-link-pattern"}});
                    }}
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
    content_loaded = content_status.get("loaded", False)
    if content_status.get("cloudflare"):
        findings["errors"].append("Cloudflare challenge blocked content extraction")
    elif not content_loaded:
        # Content not detected via selectors — give slow SPAs a bit more time
        import time

        time.sleep(10)

    listing_data_raw = _invoke_tool(
        evaluate,
        function=_LISTING_PAGE_EXTRACTION_JS,
    )
    listing_data = _parse_eval_json(listing_data_raw)
    if not listing_data:
        findings["errors"].append(f"Failed to parse listing extraction for {page_url}")

    # Content wait timed out — the links MAY be valid (SSR sites render links
    # in the initial HTML; the content wait just timed out waiting for JS
    # widgets/ads to settle) or stale (SPA didn't finish rendering). Previously
    # ALL links were discarded, which threw away valid SSR links (e.g.
    # locumtenens — 12 real job URLs discarded as "stale" → 0 product_links →
    # thin nav_analysis → broken scraper). Keep the links + flag them as
    # uncertain. Downstream (_is_product_url filter, nav_synthesize dedup,
    # code_tester) validates them.
    if not content_loaded:
        stale_count = len(listing_data.get("product_links", []))
        if stale_count > 0:
            logger.warning(
                "navigate_explore: content wait timed out — KEEPING %d product links from %s (may be valid SSR content; flagged as uncertain)",
                stale_count, page_url[:100],
            )
            listing_data["content_wait_timed_out"] = True

    findings["listing_page"].update(listing_data)
    product_count = len(listing_data.get("product_links", []))

    # Accumulate backend JSON API endpoints across ALL page visits.  SPAs
    # (React/Vue job boards, e.g. AMN) fetch listings via XHR; capturing the
    # endpoint on one page must survive a later, non-SPA page overwriting
    # listing_page.  Store deduped-by-URL in a persistent top-level list so the
    # code-writer can emit a clean api_scraper.
    page_apis = listing_data.get("api_endpoints") or []
    if page_apis:
        all_apis = findings.setdefault("api_endpoints", [])
        seen = {a.get("url") for a in all_apis}
        for api in page_apis:
            if api.get("url") and api.get("url") not in seen:
                all_apis.append(api)
                seen.add(api.get("url"))
        logger.info(
            "navigate_explore: captured %d backend API endpoint(s) on %s (accumulated %d)",
            len(page_apis), page_url[:80], len(all_apis),
        )

    if product_count > 0:
        actual_url_raw = _invoke_tool(
            evaluate, function="() => window.location.href"
        )
        url_match = re.search(r'"(https?://[^"]+)"', actual_url_raw or "")
        if url_match:
            findings["listing_page"]["url"] = url_match.group(1)

    logger.info(
        "navigate_explore: extracted %d product links from %s",
        product_count,
        page_url,
    )
    _snapshot_listing(findings)
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
            // Strategy 1: Enter keypress — triggers keydown handlers on the input
            // (many sites like AdamEve use this to build search URLs via JS)
            input.dispatchEvent(new KeyboardEvent('keydown', {{
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
            }}));
            const form = input.closest('form');
            if (form) {{
                // Strategy 2: requestSubmit fires the submit event allowing JS
                // // onsubmit handlers to run (unlike form.submit() which bypasses them).
                // Falls back to form.submit() for older browsers.
                if (typeof form.requestSubmit === 'function') {{
                    form.requestSubmit();
                }} else {{
                    form.submit();
                }}
                return 'submitted';
            }}
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
    content_loaded = content_status.get("loaded", False)
    if not content_loaded:
        time.sleep(10)

    listing_data_raw = _invoke_tool(
        evaluate,
        function=_LISTING_PAGE_EXTRACTION_JS,
    )
    listing_data = _parse_eval_json(listing_data_raw)

    # Content wait timed out — KEEP links (see comment above for rationale).
    # Previously discarded, which threw away valid SSR links.
    if not content_loaded:
        stale_count = len(listing_data.get("product_links", []))
        if stale_count > 0:
            logger.warning(
                "navigate_explore: form search content wait timed out — KEEPING %d product links (may be valid SSR content; flagged as uncertain)",
                stale_count,
            )
            listing_data["content_wait_timed_out"] = True

    findings["listing_page"].update(listing_data)

    prod_count = len(findings.get("listing_page", {}).get("product_links", []))
    logger.info(
        "navigate_explore: _try_form_search extracted %d product_links",
        prod_count,
    )
    if prod_count > 0:
        actual_url_raw = _invoke_tool(
            evaluate, function="() => window.location.href"
        )
        url_match = re.search(r'"(https?://[^"]+)"', actual_url_raw or "")
        if url_match:
            actual_url = url_match.group(1)
            findings["listing_page"]["url"] = actual_url
            # Validate the landing URL looks like a search/category results page.
            # If form.submit() redirected to a promo/landing page (e.g. AdamEve),
            # the products are from the wrong page — discard them.
            url_lower = actual_url.lower()
            search_lower = search_criteria.lower()
            is_search_like = (
                search_lower in url_lower
                or "/search?" in url_lower
                or "/search/" in url_lower
                or re.search(r'-ch-\d+', url_lower)
            )
            is_promo = any(
                kw in url_lower
                for kw in ["promo", "landing", "clearance-sale", "/sale"]
            )
            if is_promo and not is_search_like:
                discarded = len(findings["listing_page"].get("product_links", []))
                findings["listing_page"]["product_links"] = []
                findings["listing_page"]["total_products"] = None
                logger.warning(
                    "navigate_explore: form submit landed on promo page (%s), "
                    "discarding %d products",
                    actual_url[:100], discarded,
                )
            else:
                logger.info(
                    "navigate_explore: form search resolved to %s",
                    actual_url,
                )

    _snapshot_listing(findings)


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


# ── Listing-page data signal: rank candidates by content richness ──────────
# Embedded-JSON listing sites (e.g. ayahealthcare's category pages embedding a
# jobsData blob) carry the whole dataset on ONE page. The "best" listing is the
# data-richest one, NOT the first page with a few nav links. Each visited
# candidate is snapshotted; after exploration the richest snapshot is promoted
# into ``findings["listing_page"]``. Generic + conservative: only rescues a
# thin/failed listing, or promotes an embedded-JSON page over a detail-link one.

def _data_score(listing: dict) -> int:
    """Richness score for a listing_page dict.

    Embedded-JSON record count is the strongest signal (the page literally
    contains every item); otherwise fall back to the extracted product-link
    count.
    """
    if not isinstance(listing, dict):
        return 0
    emb = listing.get("embedded_json") or {}
    best = (emb.get("best") or {}) if isinstance(emb, dict) else {}
    rec = best.get("record_count") or 0
    if listing.get("data_source") == "embedded_json" and rec:
        return int(rec)
    return len(listing.get("product_links") or [])


def _snapshot_listing(findings: dict) -> None:
    """Record the current listing_page under its URL for later promotion."""
    listing = findings.get("listing_page") or {}
    if not isinstance(listing, dict) or not listing:
        return
    url = (listing.get("url") or "").strip()
    if not url:
        return
    emb = listing.get("embedded_json") or {}
    best = (emb.get("best") or {}) if isinstance(emb, dict) else {}
    snaps = findings.setdefault("_listing_snapshots", {})
    snaps[url] = {
        "score": _data_score(listing),
        "data_source": listing.get("data_source"),
        "product_link_count": len(listing.get("product_links") or []),
        "embedded_record_count": best.get("record_count", 0) if isinstance(best, dict) else 0,
        "listing": dict(listing),
    }


def _promote_data_richest_listing(findings: dict) -> None:
    """Promote the data-richest visited listing into ``findings["listing_page"]``.

    Conservative — never overrides an already-good detail-link listing unless a
    strictly-richer *embedded-JSON* page was seen (embedded data is a stronger
    "this page has the whole dataset" signal than a handful of detail links).
    This is what corrects "navigation explored a marketing page and concluded
    no items": the data-rich category page wins.
    """
    snaps = findings.get("_listing_snapshots") or {}
    if not snaps:
        return
    current = findings.get("listing_page") or {}
    cur_score = _data_score(current)
    cur_ds = current.get("data_source")
    best_url = None
    best_score = cur_score
    best_ds = cur_ds
    for url, info in snaps.items():
        s = info.get("score", 0) or 0
        if s > best_score:
            best_score = s
            best_url = url
            best_ds = info.get("data_source")
    if best_url is None or best_score < 3:
        return
    # Promote only when:
    #  (a) the current listing is thin/failed (no real data), OR
    #  (b) an embedded-JSON page beats the current listing's score.
    current_is_thin = cur_score < 3 or cur_ds in (None, "none")
    embedded_beats = best_ds == "embedded_json" and best_score > cur_score
    if not (current_is_thin or embedded_beats):
        return
    promoted = dict((snaps[best_url].get("listing")) or {})
    promoted["promoted_from"] = best_url
    findings["listing_page"] = promoted
    logger.info(
        "navigate_explore: promoted data-richest listing %s (score=%d, %s) "
        "over current (score=%d, %s)",
        best_url[:80], best_score, best_ds, cur_score, cur_ds,
    )


# ────────────────────────────────────────────────────────────────────────────
# Classic dropdown-form search (job boards, classifieds, real-estate listings)
# ────────────────────────────────────────────────────────────────────────────
# Many sites — especially JOB PORTALS — expose a dedicated "classic search"
# page with multiple <select> dropdowns (discipline/specialty, location,
# category) behind a POST form, instead of a keyword search box.  Keyword-GET
# search (STEP 3a-3c) cannot handle these: the criteria live in a server-side
# session, not the URL.  This strategy activates ONLY when keyword search
# produced no item links: it locates the search page, fills the dropdowns,
# submits, and — critically — detects the result-page filters (e.g. a "Date
# Posted" / "Last 7 Days" select) that job scraping needs.

_CLASSIC_SEARCH_LINK_RE = re.compile(
    r"(jobsearch|quicksearch|quick-?search|find-?a-?job|job-?search|"
    r"browse-?jobs?|search-?jobs?|/search/|/jobs?/search|postings?|vacanc"
    r"|search-?results?)",
    re.I,
)


def _find_classic_search_candidates(homepage_data: dict, base_url: str) -> list[str]:
    """Return ranked candidate URLs for a classic search page (Find a Job, etc.)."""
    raw_links: list = []
    for key in ("category_links", "nav_links", "all_links", "links"):
        raw_links.extend(homepage_data.get(key, []) or [])
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    for link in raw_links:
        href = (link.get("href") if isinstance(link, dict) else link) or ""
        text = (link.get("text") if isinstance(link, dict) else "") or ""
        href = (href or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            continue
        absu = urljoin(base_url, href)
        if not absu.startswith("http") or absu in seen:
            continue
        seen.add(absu)
        hay = (absu + " " + text).lower()
        score = 0
        if any(k in hay for k in ("jobsearch", "quicksearch", "quick-search", "/search")):
            score += 6
        if _CLASSIC_SEARCH_LINK_RE.search(hay):
            score += 3
        if score:
            scored.append((score, absu))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:5]]


# Detect a "classic search" form: a <form> with >= 2 populated <select>s.
_CLASSIC_FORM_DETECT_JS = r"""
() => {
  const forms = Array.from(document.querySelectorAll('form'));
  let best = null;
  for (const f of forms) {
    const selects = Array.from(f.querySelectorAll('select'))
      .filter(s => s.options && s.options.length > 2);
    if (selects.length < 2) continue;
    const infos = selects.map(s => {
      const name = s.name || s.id || '';
      const selector = s.name ? `select[name="${s.name}"]` : (s.id ? `select#${s.id}` : '');
      const options = Array.from(s.options).map(o => ({
        v: o.value || '', t: (o.textContent || '').trim()
      })).filter(o => o.t || o.v);
      return {name, selector, optionCount: s.options.length, options: options.slice(0, 80)};
    });
    const score = selects.length;
    if (!best || score > best.score) {
      best = {
        score,
        action: (f.getAttribute('action') || f.action || ''),
        method: (f.getAttribute('method') || 'get').toLowerCase(),
        selects: infos,
      };
    }
  }
  return JSON.stringify(best || {score: 0, selects: []});
}
"""


def _build_classic_fill_js(criteria: str) -> str:
    """JS that classifies each select, picks a sensible option, and submits.

    * category/specialty selects -> option whose label/value matches the search
      criteria (fallback: first non-blank, non-'Any' option).
    * location/date selects on the SEARCH form -> 'Any'/blank when available
      (broad results for discovery; the real targeting happens via the detected
      result-page filters).
    """
    crit_lit = json.dumps((criteria or "").lower())
    return (
        r"""
    () => {
      const crit = """
        + crit_lit
        + r""";
      const STATE_RE = /\b(alabama|alaska|arizona|california|texas|florida|new york|georgia|michigan|ohio|illinois|washington)\b/i;
      function classify(opts) {
        const vals = opts.map(o => (o.v || '').toLowerCase());
        const txt = opts.map(o => (o.t || '')).join(' ');
        const twoLetter = vals.filter(v => v.length === 2 && /^[a-z]{2}$/.test(v)).length;
        if (STATE_RE.test(txt) || twoLetter >= 8) return 'location';
        if (/\b(last|past|days?|weeks?|months?|any|posted)\b/i.test(txt)) return 'date';
        return 'category';
      }
      function pick(opts, kind) {
        const real = opts.filter(o => (o.t || o.v) && !/^\s*(any|all|please|select|choose)/i.test(o.t));
        if (kind === 'category') {
          let m = opts.find(o => o.t && crit && o.t.toLowerCase().includes(crit));
          if (!m) m = opts.find(o => o.v && crit && o.v.toLowerCase().includes(crit));
          if (m) return m.v;
          return (real[0] || opts[1] || opts[0] || {}).v || '';
        }
        let any = opts.find(o => /^\s*(any|all)\s*$/i.test(o.t) || o.v === '');
        if (any) return any.v;
        return (real[0] || opts[1] || opts[0] || {}).v || '';
      }
      let form = null;
      document.querySelectorAll('form').forEach(f => {
        const n = f.querySelectorAll('select').length;
        if (n >= 2 && (!form || n > form.querySelectorAll('select').length)) form = f;
      });
      if (!form) return JSON.stringify({submitted: false, reason: 'no multi-select form'});
      const fills = [];
      Array.from(form.querySelectorAll('select')).forEach(s => {
        if (!s.options || s.options.length < 2) return;
        const opts = Array.from(s.options).map(o => ({v: o.value || '', t: (o.textContent || '').trim()}));
        const kind = classify(opts);
        if (kind === 'date') return;
        const val = pick(opts, kind);
        try {
          s.value = val;
          // If value didn't take (empty value attributes — ASP.NET pattern:
          // <option value="">Text</option>), select the first real option by
          // index so the form submits with a non-empty selection.
          if (!s.value && s.options.length > 1) {
            for (let idx = 1; idx < s.options.length; idx++) {
              const txt = (s.options[idx].textContent || '').trim();
              if (txt && !/^\s*(any|all|please|select|choose)/i.test(txt)) {
                s.selectedIndex = idx;
                break;
              }
            }
          }
          s.dispatchEvent(new Event('input', {bubbles: true}));
          s.dispatchEvent(new Event('change', {bubbles: true}));
          const selOpt = s.options[s.selectedIndex] || {};
          fills.push({name: s.name || s.id, kind, value: selOpt.value || (selOpt.textContent || '').trim() || val});
        } catch (e) {}
      });
      let submitted = false;
      // Prefer clicking the submit button: ASP.NET/jQuery formValidation
      // (e.g. formValidation.js) attaches to the button click and gates the
      // submit on validation, so requestSubmit()/submit() get blocked.
      let btn = form.querySelector(
        'input[type="submit"], button[type="submit"], button:not([type])'
      );
      if (!btn) {
        btn = Array.from(form.querySelectorAll('button, input[type="button"], a.btn, a.button'))
          .find(b => /\b(search|go|find|submit|view|results?)\b/i.test((b.textContent || '') + ' ' + (b.value || '')));
      }
      let via = 'none';
      try {
        if (btn) { btn.click(); via = 'button'; }
        else if (typeof form.requestSubmit === 'function') { form.requestSubmit(); via = 'requestSubmit'; }
        else { form.submit(); via = 'submit'; }
        submitted = true;
      } catch (e) {}
      const selectDebug = Array.from(form.querySelectorAll('select')).map(s => ({
        name: s.name || s.id || '',
        optionsCount: s.options.length,
        firstOpt: s.options[0] ? (s.options[0].textContent || '').trim().slice(0, 30) : '',
        secondOpt: s.options[1] ? (s.options[1].textContent || '').trim().slice(0, 30) : '',
        secondVal: s.options[1] ? s.options[1].value : '',
      }));
      return JSON.stringify({submitted, via, fills, action: form.getAttribute('action') || '', selectDebug});
    }
    """
    )


# Detect date/location/category filter <select>s on a RESULTS page.
# For each, also capture the enclosing filter form's action + submit button so
# the generated scraper knows HOW to apply the filter (some sites auto-submit
# on change; others, e.g. LocumTenens, need an explicit "Search" button click).
_RESULT_FILTER_DETECT_JS = r"""
() => {
  const out = {date: [], location: [], category: []};
  function formInfo(s) {
    const f = s.closest('form');
    if (!f) return {};
    const btn = f.querySelector('button[type="submit"], input[type="submit"]');
    let btnSel = '';
    if (btn) {
      if (btn.id) btnSel = `#${btn.id}`;
      else if (btn.name) btnSel = `${btn.tagName.toLowerCase()}[${btn.type}][name="${btn.name}"]`;
      else btnSel = `${btn.tagName.toLowerCase()}[type="${btn.type}"]`;
    }
    return {form_id: f.id || '', form_action: (f.getAttribute('action') || f.action || ''),
            submit_button: btnSel, submit_text: btn ? (btn.textContent || btn.value || '').trim().slice(0, 24) : ''};
  }
  document.querySelectorAll('select').forEach(s => {
    const name = (s.name || s.id || '').toLowerCase();
    const opts = Array.from(s.options).map(o => ({
      v: (o.value || ''), t: (o.textContent || '').trim()
    }));
    const selector = s.name ? `select[name="${s.name}"]` : (s.id ? `select#${s.id}` : '');
    const isDate = /(jobage|age|date|posted|recent|fromage)/.test(name)
      || opts.some(o => /(last|past)\s*\d+\s*day|\d+\s*day|days?/.test((o.t || '').toLowerCase()));
    const isLoc = /(loc|state|region|city|geo|where|facility)/.test(name)
      || opts.filter(o => /^[a-z]{2}$/.test((o.v || '').toLowerCase())).length >= 6;
    const isCat = /(spec|categ|discip|profess|depart|jobtype|role|title)/.test(name);
    const entry = Object.assign(
      {selector, name: s.name || s.id || '', options: opts.slice(0, 50)},
      formInfo(s)
    );
    if (isDate && !isLoc) out.date.push(entry);
    else if (isLoc) out.location.push(entry);
    else if (isCat) out.category.push(entry);
  });
  return JSON.stringify(out);
}
"""


def _try_classic_dropdown_search(
    navigate,
    evaluate,
    homepage_data: dict,
    search_criteria: str,
    findings: dict,
    base_url: str,
) -> bool:
    """Classic dropdown-form search for sites without keyword-GET search.

    Returns True if real item links were found.  The caller only invokes this
    when keyword search (STEP 3a-3c) yielded nothing, so product sites (whose
    keyword search works) never enter this path.
    """
    import time

    candidates = _find_classic_search_candidates(homepage_data, base_url)
    # Also probe the homepage itself (some sites put the dropdown form there).
    probe_urls = candidates + ([base_url] if base_url not in candidates else [])

    search_form_found = None
    search_page_url = None
    for purl in probe_urls[:5]:
        logger.info("navigate_explore: classic-search — probing %s", purl)
        _invoke_tool(navigate, url=purl)
        time.sleep(2)
        detect_raw = _invoke_tool(evaluate, function=_CLASSIC_FORM_DETECT_JS)
        detect = _parse_eval_json(detect_raw) or {}
        n_sel = len(detect.get("selects") or [])
        if detect.get("score", 0) >= 2 and n_sel >= 2:
            search_form_found = detect
            search_page_url = purl
            logger.info(
                "navigate_explore: classic-search form detected on %s (%d selects, action=%s)",
                purl, n_sel, detect.get("action"),
            )
            break

    if not search_form_found:
        logger.info(
            "navigate_explore: classic-search — no multi-select form on any candidate page"
        )
        return False

    findings["search_attempted"] = True
    findings["homepage_nav"]["classic_search"] = {
        "url": search_page_url,
        "action": search_form_found.get("action"),
        "method": search_form_found.get("method"),
        "selects": search_form_found.get("selects"),
    }

    # Fill selects + submit.  ASP.NET/jQuery formValidation wires up async, so
    # the first button click can race and not navigate — retry the submit while
    # the URL hasn't changed away from the search form page.
    fill_js = _build_classic_fill_js(search_criteria)
    fill_raw = _invoke_tool(evaluate, function=fill_js)
    fill_result = _parse_eval_json(fill_raw) or {}
    logger.info(
        "navigate_explore: classic-search fill+submit -> %s", str(fill_result)[:500]
    )
    _sd = fill_result.get("selectDebug") if isinstance(fill_result, dict) else None
    if _sd:
        logger.info("navigate_explore: classic-search selectDebug -> %s", json.dumps(_sd)[:500])

    submit_js = r"""
    () => {
      let form = null;
      document.querySelectorAll('form').forEach(f => {
        const n = f.querySelectorAll('select').length;
        if (n >= 2 && (!form || n > form.querySelectorAll('select').length)) form = f;
      });
      if (!form) return JSON.stringify({ok: false, reason: 'no form'});
      let btn = form.querySelector('input[type="submit"], button[type="submit"], button:not([type])');
      if (!btn) btn = Array.from(form.querySelectorAll('button, input[type="button"]'))
        .find(b => /\b(search|go|find|submit|view|results?)\b/i.test((b.textContent || '') + ' ' + (b.value || '')));
      let via = 'none';
      try {
        if (btn) { btn.click(); via = 'button'; }
        else if (typeof form.requestSubmit === 'function') { form.requestSubmit(); via = 'requestSubmit'; }
        else { form.submit(); via = 'submit'; }
      } catch (e) { return JSON.stringify({ok: false, reason: String(e).slice(0, 120)}); }
      return JSON.stringify({ok: true, via});
    }
    """

    def _current_url() -> str:
        raw = _invoke_tool(evaluate, function="() => window.location.href")
        mm = re.search(r'"(https?://[^"]+)"', raw or "")
        return mm.group(1) if mm else ""

    time.sleep(3)
    for _attempt in range(4):
        cur = _current_url()
        if cur.rstrip("/").lower() != search_page_url.rstrip("/").lower():
            break  # navigated to results
        _invoke_tool(evaluate, function=submit_js)
        time.sleep(4)

    content_status = _wait_for_content(evaluate, timeout=25)
    if not content_status.get("loaded"):
        time.sleep(8)

    # Capture results URL (often session-based, e.g. ?sId=...)
    results_url = _current_url() or search_page_url

    listing_raw = _invoke_tool(evaluate, function=_LISTING_PAGE_EXTRACTION_JS)
    listing_data = _parse_eval_json(listing_raw) or {}
    # Only discard extracted links if we never left the search form page. The
    # content-wait heuristic times out on SSR results pages (ASP.NET MVC) even
    # when the job links are fully present — and we read filters fine, so the
    # page IS loaded. Trust the links whenever the URL actually changed.
    navigated = (
        results_url.rstrip("/").lower() != search_page_url.rstrip("/").lower()
    )
    if not content_status.get("loaded") and not navigated:
        listing_data["product_links"] = []
        listing_data["total_products"] = None

    findings["listing_page"] = findings.get("listing_page", {}) or {}
    findings["listing_page"].update(listing_data)
    findings["listing_page"]["url"] = results_url
    findings["listing_page"]["discovery"] = "classic_form_search"
    findings["listing_page"]["classic_search_url"] = search_page_url

    prod_count = len(findings["listing_page"].get("product_links", []))
    logger.info(
        "navigate_explore: classic-search extracted %d item links from %s",
        prod_count, results_url,
    )

    # Detect result-page filters (date / location / category selects).
    filt_raw = _invoke_tool(evaluate, function=_RESULT_FILTER_DETECT_JS)
    filt = _parse_eval_json(filt_raw) or {}
    filter_ui = {
        "date_selectors": filt.get("date", []) or [],
        "location_selectors": filt.get("location", []) or [],
        "category_selectors": filt.get("category", []) or [],
    }
    findings["listing_page"]["filter_ui"] = filter_ui
    if filter_ui["date_selectors"] or filter_ui["location_selectors"] or filter_ui["category_selectors"]:
        logger.info(
            "navigate_explore: classic-search detected result filters — date=%d loc=%d cat=%d",
            len(filter_ui["date_selectors"]),
            len(filter_ui["location_selectors"]),
            len(filter_ui["category_selectors"]),
        )

    _snapshot_listing(findings)

    found = prod_count > 0
    return found


def _do_explore_via_browser(
    tools: list,
    base_url: str,
    search_criteria: str,
    site_analysis: dict,
    search_url: str = "",
    is_job_site: bool = False,
    content_type: str = "",
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

    # ── STEP 2c: LLM pre-visit URL judgment (which candidates are real listings) ──
    # Deterministic heuristics (_cat_priority, keyword match) pick marketing pages
    # (e.g. aya's /travel-nursing/). An LLM judges candidate URLs correct vs wrong
    # from URL + anchor text + the ask (content type + query) — one cheap call, no
    # page fetched. STEP 5 then visits the judged-correct URLs first. Safe fallback
    # to the deterministic ordering if the LLM is unavailable/empty. [llm url selector]
    category_links = homepage_data.get("category_links", [])
    judged_correct_urls: list[str] = []
    try:
        from .url_judge import judge_candidate_urls, ranked_correct

        _cand_seen: set[str] = set()
        _judge_candidates: list[dict] = []
        for link in (
            list(homepage_data.get("category_links", []))
            + list(homepage_data.get("footer_links", []))
        ):
            href = (link.get("href") or "").strip() if isinstance(link, dict) else ""
            if not href or href in _cand_seen:
                continue
            if _is_non_category_link(href, link.get("text", "") if isinstance(link, dict) else ""):
                continue
            _cand_seen.add(href)
            _judge_candidates.append({"href": href, "text": (link.get("text", "") if isinstance(link, dict) else "")})
        if _judge_candidates:
            _judgment = judge_candidate_urls(
                _judge_candidates, content_type, search_criteria, base_url
            )
            findings["homepage_nav"]["llm_url_selection"] = _judgment
            judged_correct_urls = ranked_correct(_judgment, limit=8)
            logger.info(
                "navigate_explore: LLM URL selector — %d candidates, %d judged correct",
                len(_judge_candidates), len(judged_correct_urls),
            )
    except Exception as _judge_exc:
        logger.warning("navigate_explore: LLM URL selector failed: %s", _judge_exc)
        findings.setdefault("errors", []).append(f"url_judge error: {str(_judge_exc)[:120]}")

    # ── STEP 3: Search (form-based PRIMARY, URL-based SECONDARY) ────────
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

    # ── STEP 3 (job sites): Classic dropdown-form search FIRST ────────────
    # Job portals use a multi-<select> POST form (no keyword box), so the
    # keyword strategies below are futile and slow. Run classic search first;
    # only fall through to keyword search if it finds nothing.
    if is_job_site and search_criteria:
        logger.info("navigate_explore: STEP 3 (job) — classic dropdown-form search")
        try:
            if _try_classic_dropdown_search(
                navigate,
                evaluate,
                findings.get("homepage_nav", {}),
                search_criteria,
                findings,
                base_url,
            ):
                found_products = True
        except Exception as exc:
            logger.warning(
                "navigate_explore: classic-search (job-first) failed: %s",
                str(exc)[:200],
            )
            findings["errors"].append(f"classic_search error: {str(exc)[:160]}")

    # 3a: PRIMARY — Interactive form-based search (type into search box + Enter)
    form_search_count = 0
    if (
        not found_products
        and not is_job_site
        and search_criteria
        and search_form
        and search_form.get("search_input_selector")
    ):
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
    if not is_job_site and search_criteria and (not found_products or form_search_count < 30):
        findings["search_attempted"] = True
        search_urls = _build_search_urls(
            search_form, search_criteria, effective_base_url, homepage_data
        )
        if not search_urls:
            logger.info("navigate_explore: no search URLs could be built")
        for idx, surl in enumerate(search_urls[:10]):
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
    # Also attempt when URL search found very few products (<10) — the trigger-based
    # form search often yields many more results (e.g. CK UK: URL=5, form=48+93)
    current_count = len(findings.get("listing_page", {}).get("product_links", []))
    if not is_job_site and search_criteria and (not found_products or current_count < 10):
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

    # ── STEP 3d: Classic dropdown-form search (job boards / classifieds) ──
    # Activates ONLY when keyword search (3a-3c) found nothing (and not already
    # tried as the job-first strategy above). Locates a dedicated search page
    # with multiple <select> dropdowns, fills + submits the POST form, and
    # detects result-page filters (e.g. "Last 7 Days").
    if search_criteria and not found_products and not is_job_site:
        logger.info("navigate_explore: STEP 3d — classic dropdown-form search")
        try:
            if _try_classic_dropdown_search(
                navigate,
                evaluate,
                findings.get("homepage_nav", {}),
                search_criteria,
                findings,
                base_url,
            ):
                found_products = True
        except Exception as exc:
            logger.warning(
                "navigate_explore: classic-search failed: %s", str(exc)[:200]
            )
            findings["errors"].append(f"classic_search error: {str(exc)[:160]}")

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

        # PRIMARY: visit URLs the LLM judged correct (best-first). FALLBACK: the
        # deterministic _cat_priority ordering when the LLM gave nothing.
        if judged_correct_urls:
            ordered_urls = list(judged_correct_urls)
            logger.info(
                "navigate_explore: STEP 5 using LLM-judged correct URLs (%d)", len(ordered_urls)
            )
        else:
            filtered_cats.sort(key=_cat_priority)
            ordered_urls = [c.get("href", "") for c in filtered_cats[:5] if c.get("href")]

        for cat_url in ordered_urls[:5]:
            if not cat_url:
                continue
            logger.info("navigate_explore: trying category %s", cat_url)
            findings["listing_page"] = {}
            _visit_and_extract(navigate, evaluate, cat_url, cat_url, findings)
            lp = findings.get("listing_page") or {}
            # Stop on real data: embedded item JSON OR a page with real product links.
            if lp.get("data_source") == "embedded_json" or _has_real_product_links(findings):
                found_products = True
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


# ── Filter parameter classification (URL-based filtering for job portals) ──

_FILTER_DATE_KEYS = {
    "date_posted", "posted", "posteddate", "days", "fromage",
    "daterange", "date", "pd", "postedwithin", "age",
}
_FILTER_LOCATION_KEYS = {
    "location", "l", "loc", "city", "state", "st", "radius",
    "lat", "lng", "geo", "where", "region",
}
_FILTER_CATEGORY_KEYS = {
    "category", "cat", "job_type", "jt", "specialty", "discipline",
    "profession", "department", "profession_id",
}


def _classify_filter_param(key_lower: str) -> str:
    if key_lower in _FILTER_DATE_KEYS:
        return "url_date_params"
    if key_lower in _FILTER_LOCATION_KEYS:
        return "url_location_params"
    if key_lower in _FILTER_CATEGORY_KEYS:
        return "url_category_params"
    return "url_other_params"


def _enrich_filters_from_listing_url(findings: dict) -> None:
    """Populate ``listing_page.detected_filters`` from the listing URL's query.

    Covers the HTTP/BeautifulSoup fallback path where the browser JS filter
    detector does not run.  If the browser path already populated
    ``detected_filters`` with params, this is a no-op (preserves richer data).
    """
    listing = findings.get("listing_page", {}) or {}
    listing_url = listing.get("url", "")
    existing = listing.get("detected_filters") or {}
    # Skip if browser path already captured URL params
    if any(
        existing.get(bucket)
        for bucket in ("url_date_params", "url_location_params", "url_category_params")
    ):
        return
    if not listing_url:
        return
    detected: dict[str, list] = {
        "url_date_params": [],
        "url_location_params": [],
        "url_category_params": [],
        "url_other_params": [],
    }
    try:
        query = urlparse(listing_url).query
        if not query:
            return
        from urllib.parse import parse_qsl

        for key, value in parse_qsl(query, keep_blank_values=True):
            bucket = _classify_filter_param(key.lower())
            detected[bucket].append({"param": key, "value": value})
        # Merge into existing detected_filters (browser may have set empty buckets)
        merged = {**detected, **{k: v for k, v in existing.items() if v}}
        listing["detected_filters"] = merged
    except Exception as exc:
        logger.debug("navigate_explore: filter URL enrichment failed: %s", exc)


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


def _fetch_via_probe_html(url: str) -> str:
    """Fetch page HTML via browser_service /render endpoint.

    Uses the correct access method (UC Chrome for Akamai sites, Playwright
    for JS-heavy sites, direct HTTP for simple sites).  This bypasses
    Playwright MCP entirely, making it work on Akamai-protected sites.
    """
    import httpx

    service_url = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")
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

    # Job portals use a "classic" dropdown-form search instead of a keyword
    # search box; prioritize that strategy and skip the (futile, slow) keyword
    # URL enumeration for them. Product/article sites are unaffected.
    page_type = (state.get("page_type") or "").lower()
    # Source content_type from the deterministic state config (derived from
    # the job's page_type), NOT from the LLM-written site_analysis. The
    # analyzer prompt's output schema doesn't define content_type, so the LLM
    # improvises its shape run-to-run (bare string vs {"type": ...} object) —
    # reading from state eliminates that variance entirely. content_type_config
    # is populated upstream (check_tracker / setup_workspace) from page_type.
    _ct_cfg = state.get("content_type_config") or {}
    _ct = _ct_cfg.get("content_type") if isinstance(_ct_cfg, dict) else ""
    ct_type = _ct.lower() if isinstance(_ct, str) else ""
    is_job_site = page_type.startswith("job") or ct_type == "job_posting"
    if is_job_site:
        logger.info("navigate_explore: job site detected — classic search first")

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
                    is_job_site=is_job_site,
                    content_type=ct_type,
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
                            is_job_site=is_job_site,
                            content_type=ct_type,
                        )
                    finally:
                        from agents.tools.context import clear_tool_context

                        clear_tool_context()
            except Exception as exc:
                logger.warning("navigate_explore: browser retry failed: %s", exc)

    # Write findings to workspace
    findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
    os.makedirs(os.path.dirname(findings_path), exist_ok=True)

    # Enrich filter detection from the listing URL (covers HTTP fallback path)
    _enrich_filters_from_listing_url(findings)

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

    # Promote the data-richest visited listing into listing_page BEFORE the
    # rendering check (so _verify_rendering evaluates the real listing), and drop
    # the transient snapshot cache from the persisted findings.
    _promote_data_richest_listing(findings)
    findings.pop("_listing_snapshots", None)

    # Determine whether the listing page needs JS rendering (raw-HTTP vs rendered
    # item-link count). Drives _derive_strategy to pick a browser strategy upfront
    # for CSR sites instead of http_requests. [plan: smarter deterministic picker]
    _verify_rendering(findings)

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
