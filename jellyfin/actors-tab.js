/* Adult Hub UI enhancer for Jellyfin Web 12, loaded by JavaScript Injector. */
(() => {
  'use strict';

  const defaultLibraryId = ''; // Optional default Jellyfin library ItemId (inferred from URL if empty)
  const pageSize = 60;
  const actorsTabId = 'adultHubActorsTab';
  const studiosTabId = 'adultHubStudiosTab';
  const channelsTabId = 'adultHubChannelsTab';
  const viewId = 'adultHubActorsView';

  let startIndex = 0;
  let searchTerm = '';
  let selectedLetter = '';
  let onlyWithPhoto = false;
  let selectedPerson = null;
  let selectedStudio = null;
  let selectedChannel = null;
  let activeMode = null; // 'actors' | 'studios' | 'channels'
  let requestNumber = 0;
  let searchTimer;
  let lastDetailItemId = null;
  let similarScenesLoading = false;

  const diceSvg = '<svg class="ah-dice-icon" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="2.5" width="19" height="19" rx="4.5" ry="4.5" fill="none"></rect><circle cx="7.5" cy="7.5" r="1.6" fill="currentColor" stroke="none"></circle><circle cx="16.5" cy="7.5" r="1.6" fill="currentColor" stroke="none"></circle><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"></circle><circle cx="7.5" cy="16.5" r="1.6" fill="currentColor" stroke="none"></circle><circle cx="16.5" cy="16.5" r="1.6" fill="currentColor" stroke="none"></circle></svg>';

  const spinnerSvg = '<svg class="ah-dice-icon ah-spinning" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="rgba(255,255,255,0.25)"></circle><path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" stroke-linecap="round"></path></svg>';

  function getLibraryId() {
    try {
      const hash = location.hash || '';
      const q = hash.indexOf('?');
      if (q !== -1) {
        const sp = new URLSearchParams(hash.slice(q + 1));
        const id = sp.get('parentId') || sp.get('topParentId');
        if (id) return id;
      }
    } catch (_) {}
    return defaultLibraryId;
  }

  /* -------------------------------------------------------------
     INJECT GLOBAL THEME CSS (16:9 Cards, Chips, OLED Player, Neon)
     ------------------------------------------------------------- */
  const fullThemeCss = `
    /* ========================================================
       ADULT HUB — ADULT-FRIENDLY INTERFACE THEME
       ======================================================== */

    /* ========================================================
       1. KADRY I ZDJĘCIA:
          A. WYKONAWCY / OBSADA: ZAWSZE PIONOWE 2:3 (PORTRET)
          B. FILMY / SCENY: ZAWSZE POZIOME 16:9 (WIDESCREEN)
       ======================================================== */

    /* A. Zdjęcie aktora i widok profilu wykonawcy (ah-is-person / ah-person-page) */
    #itemDetailPage.ah-is-person,
    .ah-person-page {
        padding-top: 76px !important; /* Bezpieczny odstęp pod stałym paskiem nawigacji Jellyfin */
    }
    #itemDetailPage.ah-is-person #itemBackdrop,
    .ah-person-page #itemBackdrop {
        display: none !important; /* Ukryj pusty obszar backdropu - wykonawcy nie mają tła wideo */
        height: 0 !important;
        min-height: 0 !important;
    }
    #itemDetailPage.ah-is-person .detailPageWrapperContainer,
    .ah-person-page .detailPageWrapperContainer {
        margin-top: 16px !important;
        padding-left: max(3.5vw, 24px) !important;
        padding-right: max(3.5vw, 24px) !important;
    }

    /* Układ profilu wykonawcy na desktopie: dwukolumnowy CSS Grid (Lewa kolumna: zdjęcie 240px, Prawa: nagłówek i biografia) */
    @media (min-width: 769px) {
        #itemDetailPage.ah-is-person .detailPagePrimaryContainer,
        .ah-person-page .detailPagePrimaryContainer,
        .layout-desktop #itemDetailPage.ah-is-person .detailPagePrimaryContainer,
        .layout-desktop .ah-person-page .detailPagePrimaryContainer {
            display: grid !important;
            grid-template-columns: 240px minmax(0, 1fr) !important;
            grid-template-rows: auto auto !important;
            column-gap: 36px !important;
            row-gap: 12px !important;
            align-items: start !important;
            position: relative !important;
            padding: 0 !important;
        }

        #itemDetailPage.ah-is-person .detailPagePrimaryContainer > .detailImageContainer,
        .ah-person-page .detailPagePrimaryContainer > .detailImageContainer,
        .layout-desktop #itemDetailPage.ah-is-person .detailImageContainer,
        .layout-desktop .ah-person-page .detailImageContainer,
        #itemDetailPage.ah-is-person .detailImageContainer,
        .ah-person-page .detailImageContainer {
            grid-column: 1 !important;
            grid-row: 1 / span 3 !important;
            float: none !important;
            margin: 0 !important;
            position: relative !important;
            top: 0 !important;
            left: 0 !important;
            width: 240px !important;
            max-width: 240px !important;
        }

        #itemDetailPage.ah-is-person .detailRibbon,
        .ah-person-page .detailRibbon,
        .layout-desktop #itemDetailPage.ah-is-person [dir=ltr] .detailRibbon,
        .layout-desktop .ah-person-page [dir=ltr] .detailRibbon {
            grid-column: 2 !important;
            grid-row: 1 !important;
            padding: 0 !important;
            margin: 0 !important;
            width: 100% !important;
            position: relative !important;
            top: 0 !important;
            height: auto !important;
        }

        #itemDetailPage.ah-is-person .detailPagePrimaryContent,
        .ah-person-page .detailPagePrimaryContent,
        .layout-desktop #itemDetailPage.ah-is-person [dir=ltr] .detailPagePrimaryContent,
        .layout-desktop .ah-person-page [dir=ltr] .detailPagePrimaryContent {
            grid-column: 2 !important;
            grid-row: 2 !important;
            padding: 0 !important;
            margin: 0 !important;
            width: 100% !important;
            position: relative !important;
            clear: none !important;
        }
    }

    #itemDetailPage.ah-is-person .detailImageContainer .cardPadder,
    #itemDetailPage.ah-is-person .detailImageContainer .cardPadder-portrait,
    #itemDetailPage.ah-is-person .detailImageContainer .cardPadder-mixedPortrait,
    #itemDetailPage.ah-is-person .detailImageContainer .cardPadder-square,
    .ah-person-page .detailImageContainer .cardPadder-portrait,
    .ah-keep-portrait .cardPadder-portrait,
    #castContent .cardPadder-portrait,
    .personCard .cardPadder-portrait,
    [data-type="Person"] .cardPadder-portrait,
    [data-itemtype="Person"] .cardPadder-portrait,
    .card[data-type="Person"] .cardPadder-portrait,
    .card[data-type="Actor"] .cardPadder-portrait {
        padding-bottom: 145% !important; /* Klasyczny pionowy portret 2:3 */
        border-radius: 12px !important;
    }

    #itemDetailPage.ah-is-person .detailImageContainer .card,
    #itemDetailPage.ah-is-person .detailImageContainer .cardScalable,
    .ah-person-page .detailImageContainer .card {
        width: 100% !important;
        max-width: 240px !important;
        aspect-ratio: 2 / 3 !important;
        position: relative !important;
        top: 0 !important;
        left: 0 !important;
        bottom: auto !important;
        margin: 0 !important;
        float: none !important;
        border-radius: 12px !important;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(255, 255, 255, 0.12) !important;
        overflow: hidden !important;
    }
    .layout-desktop #itemDetailPage.ah-is-person .detailImageContainer .card,
    .layout-desktop .ah-person-page .detailImageContainer .card {
        width: 240px !important;
        max-width: 240px !important;
        top: 0 !important;
        left: 0 !important;
        margin: 0 !important;
        margin-bottom: 0 !important;
    }
    .layout-mobile #itemDetailPage.ah-is-person .detailImageContainer .card,
    .layout-mobile .ah-person-page .detailImageContainer .card {
        width: 170px !important;
        max-width: 48vw !important;
        top: 0 !important;
        left: 0 !important;
        margin: 0 auto !important;
    }
    #itemDetailPage.ah-is-person .detailImageContainer .cardImageContainer,
    .ah-person-page .detailImageContainer .cardImageContainer {
        border-radius: 12px !important;
        background-size: cover !important;
        background-position: center top !important; /* Kadrowanie na twarz u góry */
    }

    #itemDetailPage.ah-is-person .nameContainer,
    .ah-person-page .nameContainer {
        margin-bottom: 8px !important;
    }
    #itemDetailPage.ah-is-person .itemName,
    .ah-person-page .itemName {
        font-size: 2.2rem !important;
        font-weight: 700 !important;
        letter-spacing: -0.5px !important;
        margin: 0 0 6px 0 !important;
    }
    #itemDetailPage.ah-is-person .overview,
    .ah-person-page .overview {
        font-size: 1.02rem !important;
        line-height: 1.65 !important;
        color: rgba(255, 255, 255, 0.9) !important;
        margin-top: 8px !important;
        margin-bottom: 16px !important;
        max-width: 950px !important;
    }

    @media (max-width: 768px) {
        #itemDetailPage.ah-is-person,
        .ah-person-page {
            padding-top: 56px !important;
        }
        #itemDetailPage.ah-is-person .detailPageWrapperContainer,
        .ah-person-page .detailPageWrapperContainer {
            padding-left: 16px !important;
            padding-right: 16px !important;
        }
        #itemDetailPage.ah-is-person .detailPagePrimaryContainer,
        .ah-person-page .detailPagePrimaryContainer {
            display: flex !important;
            flex-direction: column !important;
            align-items: center !important;
        }
        #itemDetailPage.ah-is-person .detailImageContainer,
        .ah-person-page .detailImageContainer {
            float: none !important;
            margin: 0 auto 16px auto !important;
            display: flex !important;
            justify-content: center !important;
            width: 100% !important;
            grid-column: auto !important;
            grid-row: auto !important;
        }
        #itemDetailPage.ah-is-person .detailRibbon,
        .ah-person-page .detailRibbon {
            text-align: center !important;
            padding: 0 !important;
            width: 100% !important;
            grid-column: auto !important;
            grid-row: auto !important;
        }
        #itemDetailPage.ah-is-person .nameContainer,
        .ah-person-page .nameContainer {
            text-align: center !important;
        }
        #itemDetailPage.ah-is-person .mainDetailButtons,
        .ah-person-page .mainDetailButtons {
            justify-content: center !important;
        }
        #itemDetailPage.ah-is-person .detailPagePrimaryContent,
        .ah-person-page .detailPagePrimaryContent {
            padding: 14px 0 0 0 !important;
            width: 100% !important;
            text-align: left !important;
            grid-column: auto !important;
            grid-row: auto !important;
        }
    }

    /* Kafelki wykonawcow w sekcji obsady (Cast) i wyszukiwaniu */
    #castContent .portraitCard,
    #castContent .card,
    .ah-keep-portrait,
    .personCard,
    [data-type="Person"],
    [data-itemtype="Person"],
    .card[data-type="Person"],
    .card[data-type="Actor"] {
        width: 140px !important;
        min-width: 140px !important;
        max-width: 160px !important;
        flex-shrink: 0 !important;
        aspect-ratio: auto !important;
    }
    #castContent .cardImageContainer,
    .ah-keep-portrait .cardImageContainer,
    .personCard .cardImageContainer,
    [data-type="Person"] .cardImageContainer,
    .card[data-type="Person"] .cardImageContainer {
        border-radius: 8px !important;
        background-size: cover !important;
        background-position: center top !important; /* Twarz u góry */
    }
    #castContent .card {
        text-align: center !important;
    }
    #castContent .cardText-first {
        font-weight: 600 !important;
        font-size: 0.9em !important;
        margin-top: 6px !important;
    }
    #castContent .cardText-secondary {
        opacity: 0.65 !important;
        font-size: 0.82em !important;
    }

    /* B. Kadr filmu na stronie szczegółów filmu (ah-is-movie): ZAWSZE POZIOMY 16:9 */
    #itemDetailPage.ah-is-movie .detailImageContainer .cardPadder,
    #itemDetailPage.ah-is-movie .detailImageContainer .cardPadder-portrait,
    #itemDetailPage.ah-is-movie .detailImageContainer .cardPadder-mixedPortrait,
    #itemDetailPage.ah-is-movie .detailImageContainer .cardPadder-backdrop,
    #itemDetailPage.ah-is-movie .detailImageContainer .cardPadder-square,
    #itemDetailPage:not(.ah-is-person) .detailImageContainer .cardPadder-backdrop {
        padding-bottom: 56.25% !important; /* 16:9 Widescreen dla kadru filmu */
        border-radius: 8px !important;
    }
    #itemDetailPage.ah-is-movie .detailImageContainer .card,
    #itemDetailPage.ah-is-movie .detailImageContainer .cardScalable {
        width: 440px !important;
        max-width: 45vw !important;
        aspect-ratio: 16 / 9 !important;
    }
    .layout-desktop #itemDetailPage.ah-is-movie .detailImageContainer .card {
        width: 420px !important;
        max-width: 42vw !important;
        aspect-ratio: 16 / 9 !important;
    }
    .layout-mobile #itemDetailPage.ah-is-movie .detailImageContainer .card {
        width: 100% !important;
        max-width: 88vw !important;
        aspect-ratio: 16 / 9 !important;
    }
    #itemDetailPage.ah-is-movie .detailImageContainer .cardImageContainer {
        border-radius: 8px !important;
        background-size: cover !important;
        background-position: center center !important;
    }

    /* ========================================================
       2. PROPORCJE 16:9 WIDESCREEN DLA KAFELKÓW WIDEO / SCEN
          (WYŁĄCZNIE DLA FILMÓW W BIBLIOTECE I NA GŁÓWNEJ)
       ======================================================== */
    .portraitCard:not(.ah-keep-portrait):not(.personCard):not([data-type="Person"]):not([data-itemtype="Person"]):not(#castContent *):not(.ah-is-person *) .cardPadder-portrait {
        padding-bottom: 56.25% !important; /* 16:9 Widescreen dla wideo */
        border-radius: 8px !important;
    }
    .portraitCard:not(.ah-keep-portrait):not(.personCard):not([data-type="Person"]):not([data-itemtype="Person"]):not(#castContent *):not(.ah-is-person *) {
        width: 25% !important;
    }
    @media (min-width: 1600px) {
        .portraitCard:not(.ah-keep-portrait):not(.personCard):not([data-type="Person"]):not([data-itemtype="Person"]):not(#castContent *):not(.ah-is-person *) {
            width: 20% !important;
        }
    }
    @media (max-width: 1100px) {
        .portraitCard:not(.ah-keep-portrait):not(.personCard):not([data-type="Person"]):not([data-itemtype="Person"]):not(#castContent *):not(.ah-is-person *) {
            width: 33.333% !important;
        }
    }
    @media (max-width: 750px) {
        .portraitCard:not(.ah-keep-portrait):not(.personCard):not([data-type="Person"]):not([data-itemtype="Person"]):not(#castContent *):not(.ah-is-person *) {
            width: 50% !important;
        }
    }
    @media (max-width: 450px) {
        .portraitCard:not(.ah-keep-portrait):not(.personCard):not([data-type="Person"]):not([data-itemtype="Person"]):not(#castContent *):not(.ah-is-person *) {
            width: 100% !important;
        }
    }
    .cardImageContainer {
        border-radius: 8px !important;
        background-size: cover !important;
        background-position: center !important;
    }
    .cardScalable {
        border-radius: 8px !important;
        overflow: hidden !important;
        position: relative !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45) !important;
        transition: transform 0.18s ease, box-shadow 0.18s ease !important;
    }
    .cardScalable:hover {
        transform: translateY(-2px) scale(1.02) !important;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.75) !important;
    }

    /* 3. Ukrycie zbednych elementow kinowych / Hollywood */
    .detailSection-directors,
    .detailSection-writers,
    .tagline,
    .mediaInfoOfficialRating,
    .mediaInfoCriticRating,
    .criticReviewsSection {
        display: none !important;
    }

    /* 4. Nowoczesne pastylki dla Tagow, Gatunkow i Wytworni (Chips / Badges) bez etykiet i przecinkow */
    .detailsGroupItem {
        display: flex !important;
        flex-wrap: wrap !important;
        align-items: center !important;
        gap: 8px !important;
        margin: 10px 0 !important;
    }
    .detailsGroupItem .label {
        display: none !important; /* Ukryj napis 'Gatunki:' / 'Wytwórnia:' */
    }
    .detailsGroupItem .focuscontainer-x {
        display: flex !important;
        flex-wrap: wrap !important;
        align-items: center !important;
        gap: 8px !important;
    }
    .detailsGroupItem .focuscontainer-x > span {
        font-size: 0 !important;
        color: transparent !important;
        line-height: 0 !important;
        display: inline-flex !important;
    }

    .itemGenres, .itemTags {
        display: flex !important;
        flex-wrap: wrap !important;
        align-items: center !important;
        gap: 8px !important;
        margin: 10px 0 !important;
        font-size: 0 !important; /* Calkowicie ukrywa slowo 'Znaczniki:' oraz przecinki ', ' */
        color: transparent !important;
        line-height: 0 !important;
    }
    .detailsGroupItem .focuscontainer-x a.button-link,
    .itemGenres a,
    .itemTags a,
    .ah-chip-genre,
    .ah-chip-studio,
    .ah-chip-channel,
    .ah-chip-tag {
        display: inline-flex !important;
        align-items: center !important;
        border-radius: 18px !important;
        padding: 6px 14px !important;
        font-size: 0.85rem !important;
        line-height: 1.4 !important;
        font-weight: 500 !important;
        text-decoration: none !important;
        transition: all 0.18s ease !important;
        margin: 2px 0 !important;
        -webkit-backdrop-filter: blur(8px) !important;
        backdrop-filter: blur(8px) !important;
    }

    /* Gatunki (Genres): Rose / Ruby Accent */
    .detailsGroupItem.ah-group-genres .focuscontainer-x a.button-link,
    .itemGenres a,
    .ah-chip-genre {
        color: #ff7597 !important;
        border: 1px solid rgba(255, 117, 151, 0.45) !important;
        background: rgba(255, 117, 151, 0.12) !important;
    }
    .detailsGroupItem.ah-group-genres .focuscontainer-x a.button-link:hover,
    .itemGenres a:hover,
    .ah-chip-genre:hover {
        background: #ff7597 !important;
        border-color: #ff7597 !important;
        color: #111 !important;
        box-shadow: 0 0 12px rgba(255, 117, 151, 0.5) !important;
        transform: translateY(-1px) !important;
    }

    /* Wytwornie (Studios): Warm Amber / Gold Accent */
    .detailsGroupItem.ah-group-studios .focuscontainer-x a.button-link,
    .ah-chip-studio {
        color: #ffca3a !important;
        border: 1px solid rgba(255, 202, 58, 0.45) !important;
        background: rgba(255, 202, 58, 0.12) !important;
    }
    .detailsGroupItem.ah-group-studios .focuscontainer-x a.button-link:hover,
    .ah-chip-studio:hover {
        background: #ffca3a !important;
        border-color: #ffca3a !important;
        color: #111 !important;
        box-shadow: 0 0 12px rgba(255, 202, 58, 0.5) !important;
        transform: translateY(-1px) !important;
    }

    /* Kanaly Telegram: Electric Cyan / Blue Accent */
    .ah-chip-channel {
        color: #70d6ff !important;
        border: 1px solid rgba(112, 214, 255, 0.45) !important;
        background: rgba(112, 214, 255, 0.12) !important;
    }
    .ah-chip-channel:hover {
        background: #70d6ff !important;
        border-color: #70d6ff !important;
        color: #111 !important;
        box-shadow: 0 0 12px rgba(112, 214, 255, 0.5) !important;
        transform: translateY(-1px) !important;
    }

    /* Inne pastylki / tagi: Frosted Pearl Neutral */
    .ah-chip-tag {
        color: #eaeaea !important;
        border: 1px solid rgba(255, 255, 255, 0.22) !important;
        background: rgba(255, 255, 255, 0.09) !important;
    }
    .ah-chip-tag:hover {
        background: rgba(255, 255, 255, 0.25) !important;
        border-color: rgba(255, 255, 255, 0.5) !important;
        color: #fff !important;
        transform: translateY(-1px) !important;
    }

    /* 5. Czas trwania i czytelnosc napisow na kafelkach */
    .cardScalable .cardOverlayContainer {
        background: linear-gradient(0deg, rgba(0,0,0,0.75) 0%, transparent 60%) !important;
    }
    .cardText-first {
        font-weight: 600 !important;
        font-size: 0.95rem !important;
        color: #fff !important;
    }
    .cardText-secondary {
        color: #bbb !important;
        font-size: 0.85rem !important;
    }

    /* 6. Pasek postepu (Progress Bar) - Neon Orange-Pink Gradient */
    .itemProgressBar {
        background: rgba(255, 255, 255, 0.22) !important;
        height: 4px !important;
        border-radius: 2px !important;
        overflow: hidden !important;
    }
    .itemProgressBarForeground {
        background: linear-gradient(90deg, #ff0055, #ff5500) !important;
        box-shadow: 0 0 8px rgba(255, 0, 85, 0.8) !important;
        height: 100% !important;
    }

    /* 7. True Black OLED Video Player */
    .videoPlayerContainer,
    .nowPlayingPage,
    .nowPlayingPage-nowPlaying,
    .videoOsdBottom,
    .videoOsdTop,
    .videoBackdrop {
        background-color: #000000 !important;
    }
    .videoOsdBottom {
        background: linear-gradient(0deg, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.6) 70%, transparent 100%) !important;
    }
    .videoOsdTop {
        background: linear-gradient(180deg, rgba(0,0,0,0.85) 0%, transparent 100%) !important;
    }

    /* 8. Przyciski nawigacyjne Adult Hub w gornym naglowku */
    .ah-header-btn {
        background: rgba(255, 255, 255, 0.12) !important;
        border: 1px solid rgba(255, 255, 255, 0.25) !important;
        border-radius: 18px !important;
        color: #fff !important;
        padding: 5px 14px !important;
        margin: 0 4px !important;
        font-size: 0.85rem !important;
        cursor: pointer !important;
        font-weight: 600 !important;
        display: inline-flex !important;
        align-items: center !important;
        gap: 5px !important;
        transition: all 0.2s ease !important;
    }
    .ah-header-btn:hover {
        background: var(--theme-primary-color, #00a4dc) !important;
        border-color: var(--theme-primary-color, #00a4dc) !important;
        transform: translateY(-1px) !important;
    }

    /* 9. Widok przegladarki Wykonawcow, Wytworni i Kanalow */
    #${actorsTabId}, #${studiosTabId}, #${channelsTabId} { cursor: pointer; }
    #${actorsTabId}.adultHubActorsActive, #${studiosTabId}.adultHubActorsActive, #${channelsTabId}.adultHubActorsActive {
        color: var(--theme-primary-color, #00a4dc);
        border-bottom: 2px solid currentColor;
    }
    /* Ukrywanie pod-nawigacji i stronicowania biblioteki kiedy widok Adult Hub jest otwarty */
    body.ah-view-open .skinHeader .headerTabs,
    body.ah-view-open .skinHeader .sectionTabs,
    body.ah-view-open .skinHeader .pageTitle,
    body.ah-view-open .skinHeader .viewMenuBar,
    body.ah-view-open .skinHeader .libraryViewNav,
    body.ah-view-open .mainAnimatedPage > .MuiAppBar-root,
    body.ah-view-open .paging,
    body.ah-view-open .listPaging,
    body.ah-view-open #moviesPage .pageTabContent > .flex:first-child,
    body.ah-view-open .pageTabContent > .flex:first-child {
        display: none !important;
    }

    #${viewId} {
        position: fixed !important;
        inset: 54px 0 0 !important;
        z-index: 1000 !important;
        overflow-x: hidden !important;
        overflow-y: auto !important;
        background-color: #101010 !important;
        background: #101010 !important;
        color: #fff !important;
        padding: 24px max(3vw, 20px) 60px !important;
        box-sizing: border-box !important;
    }
    #${viewId} * { box-sizing: border-box; }
    #${viewId} .ah-head {
        display: flex !important;
        align-items: center !important;
        flex-wrap: wrap !important;
        gap: 14px !important;
        margin: 0 0 18px !important;
        padding: 4px 0 !important;
        position: relative !important;
        z-index: 2 !important;
    }
    #${viewId} h2 { margin: 0 18px 0 0; font-size: 1.75rem; font-weight: 700; }
    #${viewId} .ah-performer-avatar {
        width: 52px;
        height: 52px;
        border-radius: 50%;
        object-fit: cover;
        object-position: center top;
        border: 2px solid rgba(255, 255, 255, 0.25);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
        flex-shrink: 0;
        margin: 0 4px 0 0;
    }
    #${viewId} input { width: min(380px, 100%); padding: 10px 16px; border: 1px solid #555; border-radius: 8px; background: #222; color: #fff; font: inherit; }
    #${viewId} .ah-alphabet { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 20px; }
    #${viewId} .ah-letter-btn { background: #242424; border: 1px solid #444; color: #eee; border-radius: 6px; padding: 5px 10px; font-size: 0.85rem; cursor: pointer; min-width: 30px; text-align: center; }
    #${viewId} .ah-letter-btn:hover, #${viewId} .ah-letter-btn.ah-active { background: var(--theme-primary-color, #00a4dc); border-color: var(--theme-primary-color, #00a4dc); color: #fff; font-weight: bold; }
    
    /* Siatka wykonawcow (2:3 Portrait) */
    #${viewId} .ah-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(135px, 1fr)); gap: 18px; }
    #${viewId} .ah-card { min-width: 0; padding: 0; border: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; font: inherit; }
    #${viewId} .ah-card img, #${viewId} .ah-placeholder { width: 100%; aspect-ratio: 2 / 3; object-fit: cover; object-position: center top !important; border-radius: 8px; background: #333; display: block; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
    #${viewId} .ah-placeholder { display: grid; place-items: center; font-size: 2rem; }
    #${viewId} .ah-card span { display: block; padding: 8px 2px; overflow-wrap: anywhere; font-size: 0.92rem; font-weight: 500; }
    #${viewId} .ah-card:hover span, #${viewId} .ah-card:focus-visible span { text-decoration: underline; color: var(--theme-primary-color, #00a4dc); }
    
    /* Siatka wytworni / kanalow */
    #${viewId} .ah-studio-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 18px; }
    #${viewId} .ah-studio-card { background: #202020; border: 1px solid #383838; border-radius: 10px; padding: 22px 16px; text-align: center; cursor: pointer; color: inherit; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 120px; transition: transform 0.15s, border-color 0.15s; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
    #${viewId} .ah-studio-card:hover { transform: translateY(-3px); border-color: var(--theme-primary-color, #00a4dc); background: #282828; }
    #${viewId} .ah-studio-icon { font-size: 2.4rem; margin-bottom: 10px; }
    #${viewId} .ah-studio-name { font-size: 1rem; font-weight: 600; line-height: 1.35; }

    /* 10. Siatka i karty filmow (16:9 Widescreen Movies Grid) */
    .ah-movie-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 20px;
    }
    @media (max-width: 600px) {
        .ah-movie-grid {
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 14px;
        }
    }
    .ah-movie-card {
        display: flex;
        flex-direction: column;
        cursor: pointer;
        background: transparent;
        border: none;
        padding: 0;
        text-align: left;
        color: inherit;
        position: relative;
    }
    .ah-movie-thumb {
        position: relative;
        width: 100%;
        aspect-ratio: 16 / 9;
        border-radius: 8px;
        overflow: hidden;
        background: #1c1c1c;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .ah-movie-card:hover .ah-movie-thumb {
        transform: translateY(-3px) scale(1.02);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.8);
    }
    .ah-movie-thumb img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
    }
    .ah-movie-thumb .ah-placeholder {
        aspect-ratio: 16 / 9;
    }
    .ah-quick-play {
        position: absolute;
        inset: 0;
        margin: auto;
        width: 48px;
        height: 48px;
        border-radius: 50% !important;
        background-color: rgba(0, 0, 0, 0.55) !important;
        border: 2px solid rgba(255, 255, 255, 0.75) !important;
        color: #fff !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        opacity: 0;
        transform: scale(0.85);
        transition: opacity 0.2s ease, transform 0.2s ease, background-color 0.2s ease, border-color 0.2s ease !important;
        cursor: pointer;
        z-index: 2;
        padding: 0 !important;
        box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5) !important;
    }
    .ah-quick-play .material-icons,
    .ah-quick-play .cardOverlayButtonIcon {
        font-size: 28px !important;
        width: 28px !important;
        height: 28px !important;
        line-height: 28px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        color: #fff !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    .ah-movie-card:hover .ah-quick-play {
        opacity: 1;
        transform: scale(1);
    }
    .ah-quick-play:hover {
        transform: scale(1.18) !important;
        background-color: rgba(0, 0, 0, 0.82) !important;
        border-color: #ffffff !important;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.75) !important;
        color: #fff !important;
    }
    .ah-badge {
        position: absolute;
        background: rgba(14, 16, 22, 0.42) !important;
        -webkit-backdrop-filter: blur(12px) saturate(150%) !important;
        backdrop-filter: blur(12px) saturate(150%) !important;
        color: #fff !important;
        font-size: 0.73rem !important;
        font-weight: 700 !important;
        padding: 3px 8px !important;
        border-radius: 5px !important;
        letter-spacing: 0.3px !important;
        box-shadow: 0 3px 10px rgba(0, 0, 0, 0.45) !important;
        z-index: 1;
    }
    .ah-badge-duration {
        bottom: 8px;
        right: 8px;
        background: rgba(14, 16, 22, 0.42) !important;
        border: 1px solid rgba(255, 255, 255, 0.22) !important;
        color: #ffffff !important;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.95) !important;
    }
    .ah-badge-res {
        top: 8px;
        right: 8px;
        background: rgba(14, 16, 22, 0.42) !important;
        border: 1px solid rgba(112, 214, 255, 0.55) !important;
        color: #70d6ff !important;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.95) !important;
    }
    .ah-badge-genre {
        top: 8px;
        left: 8px;
        background: rgba(14, 16, 22, 0.38) !important;
        -webkit-backdrop-filter: blur(12px) saturate(150%) !important;
        backdrop-filter: blur(12px) saturate(150%) !important;
        border: 1px solid rgba(255, 255, 255, 0.25) !important;
        border-left: 3.5px solid #00a4dc !important;
        color: #ffffff !important;
        font-size: 0.73rem !important;
        font-weight: 700 !important;
        letter-spacing: 0.35px !important;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.95) !important;
        box-shadow: 0 3px 10px rgba(0, 0, 0, 0.45) !important;
        padding: 3px 8px !important;
        border-radius: 4px !important;
        max-width: 65% !important;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .ah-movie-thumb .itemProgressBar {
        position: absolute;
        bottom: 0;
        left: 0;
        right: 0;
        height: 4px;
        border-radius: 0;
        z-index: 2;
    }
    .ah-movie-title {
        margin-top: 9px;
        font-size: 0.95rem;
        font-weight: 600;
        color: #eee;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        line-height: 1.3;
    }
    .ah-movie-card:hover .ah-movie-title {
        color: var(--theme-primary-color, #00a4dc);
    }
    .ah-movie-sub {
        margin-top: 3px;
        font-size: 0.82rem;
        color: #999;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    /* 11. Pasek filtrowania i sortowania (Toolbar) */
    .ah-toolbar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin: 14px 0 20px;
        padding: 10px 16px;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
    }
    .ah-toolbar-group {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
    }
    .ah-toolbar-label {
        font-size: 0.8rem;
        color: #888;
        text-transform: uppercase;
        font-weight: 600;
        margin-right: 2px;
        letter-spacing: 0.5px;
    }
    .ah-filter-chips {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
    }
    .ah-chip-btn {
        background: #242424;
        border: 1px solid #444;
        color: #ccc;
        border-radius: 16px;
        padding: 5px 14px;
        font-size: 0.82rem;
        cursor: pointer;
        font-weight: 500;
        transition: all 0.15s ease;
    }
    .ah-chip-btn:hover, .ah-chip-btn.ah-active {
        background: var(--theme-primary-color, #00a4dc);
        border-color: var(--theme-primary-color, #00a4dc);
        color: #fff;
        font-weight: 600;
    }

    /* Przelacznik ze zdjeciem */
    .ah-filter-toggle.ah-active {
        background: var(--theme-primary-color, #00a4dc) !important;
        border-color: var(--theme-primary-color, #00a4dc) !important;
        color: #fff !important;
    }

    /* Przycisk losowania nastepnej sceny w odtwarzaczu OSD (🎲) */
    .ah-osd-random-btn {
        background: transparent !important;
        border: 0 !important;
        border-radius: 50% !important;
        box-shadow: none !important;
        color: inherit !important;
        cursor: pointer !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        margin: 0 .29em !important;
        padding: .556em !important;
        width: auto !important;
        height: auto !important;
        min-width: 0 !important;
        min-height: 0 !important;
        outline: none !important;
        transition: background-color 0.2s ease, color 0.2s ease !important;
        vertical-align: middle !important;
        flex-shrink: 0 !important;
        box-sizing: border-box !important;
    }
    @media (hover: hover) and (pointer: fine) {
        .ah-osd-random-btn:hover:not(:disabled) {
            background-color: rgba(0, 164, 220, 0.2) !important;
            color: var(--jf-palette-primary-main, var(--theme-primary-color, #00a4dc)) !important;
            border: 0 !important;
            box-shadow: none !important;
        }
    }
    .ah-osd-random-btn:focus-visible,
    .ah-osd-random-btn.show-focus:focus {
        background-color: rgba(0, 164, 220, 0.2) !important;
        color: var(--jf-palette-primary-main, var(--theme-primary-color, #00a4dc)) !important;
        outline: none !important;
    }
    .ah-osd-random-btn svg {
        display: block !important;
        width: 1.6695652174em !important;
        height: 1.6695652174em !important;
        max-width: 26px !important;
        max-height: 26px !important;
        pointer-events: none !important;
        color: inherit !important;
    }
    .ah-header-btn svg {
        width: 16px !important;
        height: 16px !important;
        pointer-events: none !important;
        vertical-align: middle !important;
    }
    @keyframes ah-spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }
    .ah-spinning {
        animation: ah-spin 0.7s linear infinite !important;
    }

    /* 12. Sekcja podobnych scen na stronie szczegolow filmu */
    .ah-similar-section {
        margin: 32px 0 20px;
        padding-top: 20px;
        border-top: 1px solid rgba(255, 255, 255, 0.12);
    }
    .ah-similar-heading {
        font-size: 1.35rem;
        font-weight: 700;
        margin: 0 0 16px;
        color: #fff;
    }

    /* Kontrolki stronicowania */
    #${viewId} .ah-controls { display: flex; align-items: center; justify-content: center; gap: 14px; margin: 30px 0; }
    #${viewId} button.ah-action { padding: 9px 18px; border: 1px solid #666; border-radius: 8px; background: #303030; color: #fff; cursor: pointer; font: inherit; font-weight: 500; }
    #${viewId} button.ah-action:disabled { opacity: .45; cursor: default; }
    @media (max-width: 700px) { #${viewId} { inset: 56px 0 0; } #${viewId} .ah-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 12px; } }
  `;

  let styleTag = document.getElementById('adultHubThemeStyle');
  if (!styleTag) {
    styleTag = document.createElement('style');
    styleTag.id = 'adultHubThemeStyle';
    document.head.appendChild(styleTag);
  }
  styleTag.textContent = fullThemeCss;

  function api() { return window.ApiClient; }
  function query(path, params) {
    const client = api();
    if (!client || typeof client.getJSON !== 'function') throw new Error('Jellyfin API unavailable');
    return client.getJSON(client.getUrl(path, params));
  }

  function imageUrl(item, width) {
    if (!item) return '';
    const tag = (item.ImageTags && item.ImageTags.Primary) || item.PrimaryImageTag;
    if (!tag) return '';
    try {
      return api().getImageUrl(item.Id, { type: 'Primary', maxWidth: width, tag: tag });
    } catch (_) {
      return `/Items/${item.Id}/Images/Primary?maxWidth=${width}&tag=${tag}`;
    }
  }

  function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = value;
    return node;
  }

  function button(label, fn) {
    const node = el('button', 'ah-action', label);
    node.type = 'button';
    node.addEventListener('click', fn);
    return node;
  }

  function view() { return document.getElementById(viewId); }

  function clearView() {
    document.body.classList.remove('ah-view-open');
    clearTimeout(searchTimer);
    requestNumber++;
    view()?.remove();
    document.getElementById(actorsTabId)?.classList.remove('adultHubActorsActive');
    document.getElementById(studiosTabId)?.classList.remove('adultHubActorsActive');
    document.getElementById(channelsTabId)?.classList.remove('adultHubActorsActive');
    selectedPerson = null;
    selectedStudio = null;
    selectedChannel = null;
    activeMode = null;
    lastDetailItemId = null;
  }

  function root(mode) {
    activeMode = mode;
    document.body.classList.add('ah-view-open');
    let node = view();
    if (!node) {
      node = el('section');
      node.id = viewId;
      document.body.appendChild(node);
    }
    document.getElementById(actorsTabId)?.classList.toggle('adultHubActorsActive', mode === 'actors');
    document.getElementById(studiosTabId)?.classList.toggle('adultHubActorsActive', mode === 'studios');
    document.getElementById(channelsTabId)?.classList.toggle('adultHubActorsActive', mode === 'channels');
    node.setAttribute('aria-label', mode === 'studios' ? 'Wytwórnie' : (mode === 'channels' ? 'Kanały' : 'Wykonawcy'));

    const header = document.querySelector('.headerTop') || document.querySelector('.skinHeader') || document.querySelector('header');
    if (header) {
      node.style.top = Math.max(50, Math.ceil(header.getBoundingClientRect().bottom)) + 'px';
    }
    node.replaceChildren();
    return node;
  }

  function formatDuration(ticks) {
    if (!ticks) return '';
    const totalSeconds = Math.floor(ticks / 10000000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    }
    return `${Math.max(1, minutes)} min`;
  }

  function detectResolution(item) {
    if (item.Width) {
      if (item.Width >= 3840) return '4K';
      if (item.Width >= 1920) return '1080p';
      if (item.Width >= 1280) return '720p';
    }
    if (item.MediaStreams) {
      for (const s of item.MediaStreams) {
        if (s.Type === 'Video' && s.Width) {
          if (s.Width >= 3840) return '4K';
          if (s.Width >= 1920) return '1080p';
          if (s.Width >= 1280) return '720p';
        }
      }
    }
    const txt = ((item.Name || '') + ' ' + (item.Path || '')).toLowerCase();
    if (txt.includes('4k') || txt.includes('2160p')) return '4K';
    if (txt.includes('1080p') || txt.includes('fhd')) return '1080p';
    if (txt.includes('720p') || txt.includes('hd')) return '720p';
    return '';
  }

  let pmInstance = null;

  function getPlaybackManager() {
    if (pmInstance && typeof pmInstance.play === 'function') {
      return Promise.resolve(pmInstance);
    }
    if (window.playbackManager && typeof window.playbackManager.play === 'function') {
      pmInstance = window.playbackManager;
      return Promise.resolve(pmInstance);
    }

    return new Promise((resolve) => {
      try {
        const chunk = window.webpackChunk || self.webpackChunk;
        if (chunk && typeof chunk.push === 'function') {
          const chunkId = 'ah-pm-' + Math.random().toString(36).substring(2, 9);
          chunk.push([
            [chunkId],
            {},
            function(__webpack_require__) {
              try {
                // Moduł 68221 to playbackManager w Jellyfin Web
                const mod = __webpack_require__(68221);
                const pm = mod && (mod.f || mod.default || mod);
                if (pm && typeof pm.play === 'function') {
                  pmInstance = pm;
                  window.playbackManager = pm;
                  return resolve(pm);
                }
              } catch (e) {
                console.warn('[Adult Hub] Webpack require(68221) failed:', e);
              }

              // Skanowanie cache modułów webpacka
              try {
                if (__webpack_require__.c) {
                  for (const modId in __webpack_require__.c) {
                    const exports = __webpack_require__.c[modId]?.exports;
                    const candidate = exports?.f || exports?.default || exports;
                    if (candidate && typeof candidate.play === 'function' && typeof candidate.getCurrentPlayer === 'function') {
                      pmInstance = candidate;
                      window.playbackManager = candidate;
                      return resolve(candidate);
                    }
                  }
                }
              } catch (e) {
                console.warn('[Adult Hub] Webpack cache scan failed:', e);
              }

              resolve(null);
            }
          ]);
          return;
        }
      } catch (err) {
        console.warn('[Adult Hub] Webpack chunk hook failed:', err);
      }
      resolve(null);
    });
  }

  // Wstępna inicjalizacja PlaybackManager w tle
  getPlaybackManager();

  async function playMovie(id, e) {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    try {
      const pm = await getPlaybackManager();
      if (pm && typeof pm.play === 'function') {
        const item = await query(`Users/${getUserId()}/Items/${id}`);
        if (item) {
          await pm.play({ items: [item], startPositionTicks: 0 });
          return;
        }
        await pm.play({ ids: [id], serverId: (window.ApiClient && window.ApiClient.serverId && window.ApiClient.serverId()) || undefined, startPositionTicks: 0 });
        return;
      }
    } catch (err) {
      console.warn('[Adult Hub] playMovie direct playback error:', err);
    }
    // Awaryjnie: przejdz bezposrednio do playera wideo (nie do strony szczegółów!)
    window.location.hash = `#/playback/video?id=${id}`;
  }

  function performerCard(person, onClick) {
    const node = el('button', 'ah-card');
    node.type = 'button';
    const url = imageUrl(person, 250);
    if (url) {
      const img = el('img');
      img.src = url;
      img.alt = '';
      img.loading = 'lazy';
      node.appendChild(img);
    } else {
      node.appendChild(el('div', 'ah-placeholder', '👤'));
    }
    node.appendChild(el('span', '', person.Name || 'Bez nazwy'));
    node.addEventListener('click', onClick);
    return node;
  }

  function movieCard(item) {
    const card = el('div', 'ah-movie-card');
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.addEventListener('click', () => goToItem(item.Id));
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        goToItem(item.Id);
      }
    });

    const thumb = el('div', 'ah-movie-thumb');
    const url = imageUrl(item, 450);
    if (url) {
      const img = el('img');
      img.src = url;
      img.alt = item.Name || '';
      img.loading = 'lazy';
      thumb.appendChild(img);
    } else {
      thumb.appendChild(el('div', 'ah-placeholder', '▶'));
    }

    // Quick play button z identyczną ikoną play_arrow jak w Jellyfin
    const playBtn = el('button', 'ah-quick-play paper-icon-button-light cardOverlayButton cardOverlayButton-hover cardOverlayFab-primary cardOverlayButton-centered');
    playBtn.type = 'button';
    playBtn.title = 'Odtwórz film';
    playBtn.setAttribute('aria-label', 'Odtwórz film');
    playBtn.innerHTML = '<span class="material-icons cardOverlayButtonIcon play_arrow" aria-hidden="true"></span>';
    playBtn.addEventListener('click', (e) => playMovie(item.Id, e));
    thumb.appendChild(playBtn);

    // Genre badge
    if (item.Genres && item.Genres.length > 0) {
      const genreBadge = el('span', 'ah-badge ah-badge-genre', item.Genres[0]);
      genreBadge.title = item.Genres[0];
      thumb.appendChild(genreBadge);
    }

    // Duration badge
    const durStr = formatDuration(item.RunTimeTicks);
    if (durStr) {
      const durBadge = el('span', 'ah-badge ah-badge-duration', durStr);
      thumb.appendChild(durBadge);
    }

    // Resolution badge
    const resStr = detectResolution(item);
    if (resStr) {
      const resBadge = el('span', 'ah-badge ah-badge-res', resStr);
      thumb.appendChild(resBadge);
    }

    // Progress bar
    if (item.UserData && item.UserData.PlaybackPositionTicks && item.RunTimeTicks) {
      const percent = Math.min(100, Math.round((item.UserData.PlaybackPositionTicks / item.RunTimeTicks) * 100));
      if (percent > 0) {
        const bar = el('div', 'itemProgressBar');
        const fill = el('div', 'itemProgressBarForeground');
        fill.style.width = percent + '%';
        bar.appendChild(fill);
        thumb.appendChild(bar);
      }
    }

    card.appendChild(thumb);

    // Title
    const title = el('div', 'ah-movie-title', item.Name || 'Bez tytułu');
    title.title = item.Name || '';
    card.appendChild(title);

    // Subtitle (performers or year)
    let subText = '';
    if (item.People && item.People.length > 0) {
      subText = item.People.map(p => p.Name).slice(0, 3).join(' • ');
    } else if (item.ProductionYear) {
      subText = String(item.ProductionYear);
    }
    if (subText) {
      card.appendChild(el('div', 'ah-movie-sub', subText));
    }

    return card;
  }

  function createFilterAndSortBar(allMovies, onUpdate) {
    const toolbar = el('div', 'ah-toolbar');
    let currentDuration = 'all';
    let currentSort = 'default';

    // 1. Filtry czasu trwania
    const durGroup = el('div', 'ah-toolbar-group');
    durGroup.appendChild(el('span', 'ah-toolbar-label', 'Czas:'));

    const durFilters = [
      { id: 'all', label: 'Wszystkie' },
      { id: 'short', label: '< 15 min' },
      { id: 'medium', label: '15–30 min' },
      { id: 'long', label: '> 30 min' }
    ];

    for (const df of durFilters) {
      const btn = el('button', 'ah-chip-btn' + (df.id === 'all' ? ' ah-active' : ''), df.label);
      btn.type = 'button';
      btn.dataset.duration = df.id;
      btn.addEventListener('click', () => {
        currentDuration = df.id;
        durGroup.querySelectorAll('.ah-chip-btn').forEach(b => b.classList.toggle('ah-active', b.dataset.duration === df.id));
        applyFiltersAndSort();
      });
      durGroup.appendChild(btn);
    }
    toolbar.appendChild(durGroup);

    // 2. Opcje sortowania
    const sortGroup = el('div', 'ah-toolbar-group');
    sortGroup.appendChild(el('span', 'ah-toolbar-label', 'Sortuj:'));

    const sortOptions = [
      { id: 'default', label: 'Domyślnie' },
      { id: 'newest', label: '📅 Najnowsze' },
      { id: 'longest', label: '⏳ Najdłuższe' },
      { id: 'random', label: '🎲 Losowo' },
      { id: 'name', label: '🔤 A–Z' }
    ];

    for (const so of sortOptions) {
      const btn = el('button', 'ah-chip-btn' + (so.id === 'default' ? ' ah-active' : ''), so.label);
      btn.type = 'button';
      btn.dataset.sort = so.id;
      btn.addEventListener('click', () => {
        currentSort = so.id;
        sortGroup.querySelectorAll('.ah-chip-btn').forEach(b => b.classList.toggle('ah-active', b.dataset.sort === so.id));
        applyFiltersAndSort();
      });
      sortGroup.appendChild(btn);
    }
    toolbar.appendChild(sortGroup);

    function applyFiltersAndSort() {
      let filtered = allMovies;
      if (currentDuration === 'short') {
        filtered = allMovies.filter(m => m.RunTimeTicks && m.RunTimeTicks < 15 * 60 * 10000000);
      } else if (currentDuration === 'medium') {
        filtered = allMovies.filter(m => m.RunTimeTicks && m.RunTimeTicks >= 15 * 60 * 10000000 && m.RunTimeTicks <= 30 * 60 * 10000000);
      } else if (currentDuration === 'long') {
        filtered = allMovies.filter(m => m.RunTimeTicks && m.RunTimeTicks > 30 * 60 * 10000000);
      }

      const sorted = [...filtered];
      if (currentSort === 'newest') {
        sorted.sort((a, b) => new Date(b.DateCreated || 0) - new Date(a.DateCreated || 0));
      } else if (currentSort === 'longest') {
        sorted.sort((a, b) => (b.RunTimeTicks || 0) - (a.RunTimeTicks || 0));
      } else if (currentSort === 'random') {
        for (let i = sorted.length - 1; i > 0; i--) {
          const j = Math.floor(Math.random() * (i + 1));
          [sorted[i], sorted[j]] = [sorted[j], sorted[i]];
        }
      } else if (currentSort === 'name') {
        sorted.sort((a, b) => (a.SortName || a.Name || '').localeCompare(b.SortName || b.Name || ''));
      }

      onUpdate(sorted);
    }

    return { toolbar, applyFiltersAndSort };
  }

  function goToItem(id) {
    clearView();
    const serverId = typeof api()?.serverId === 'function' ? api().serverId() : '';
    const params = new URLSearchParams({ id });
    if (serverId) params.set('serverId', serverId);
    location.hash = '#/details?' + params.toString();
  }

  function error(node, err) {
    node.replaceChildren(el('p', '', 'Nie udało się pobrać danych z Jellyfin.'));
    console.error('[Adult Hub UI]', err);
  }

  /* -------------------------------------------------------------
     1. WYKONAWCY (ACTORS / PERFORMERS)
     ------------------------------------------------------------- */
  async function showActors() {
    selectedPerson = null;
    selectedStudio = null;
    selectedChannel = null;
    const current = ++requestNumber;
    const page = root('actors');
    const head = el('div', 'ah-head');
    head.appendChild(button('← Wróć', clearView));
    head.appendChild(el('h2', '', 'Wykonawcy'));

    const input = el('input');
    input.type = 'search';
    input.placeholder = 'Szukaj wykonawcy…';
    input.setAttribute('aria-label', 'Szukaj wykonawcy');
    input.value = searchTerm;
    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        searchTerm = input.value.trim();
        startIndex = 0;
        showActors();
      }, 300);
    });
    head.appendChild(input);

    // Przelacznik "📷 Ze zdjeciem"
    const photoFilterBtn = el('button', 'ah-action ah-filter-toggle' + (onlyWithPhoto ? ' ah-active' : ''), '📷 Ze zdjęciem');
    photoFilterBtn.type = 'button';
    photoFilterBtn.addEventListener('click', () => {
      onlyWithPhoto = !onlyWithPhoto;
      photoFilterBtn.classList.toggle('ah-active', onlyWithPhoto);
      startIndex = 0;
      showActors();
    });
    head.appendChild(photoFilterBtn);

    page.appendChild(head);

    const alphaContainer = el('div', 'ah-alphabet');
    const allBtn = el('button', 'ah-letter-btn' + (!selectedLetter ? ' ah-active' : ''), 'Wszystkie');
    allBtn.type = 'button';
    allBtn.addEventListener('click', () => {
      selectedLetter = '';
      startIndex = 0;
      showActors();
    });
    alphaContainer.appendChild(allBtn);

    const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
    for (const l of letters) {
      const btn = el('button', 'ah-letter-btn' + (selectedLetter === l ? ' ah-active' : ''), l);
      btn.type = 'button';
      btn.addEventListener('click', () => {
        selectedLetter = l;
        startIndex = 0;
        showActors();
      });
      alphaContainer.appendChild(btn);
    }
    page.appendChild(alphaContainer);

    const status = el('p', '', 'Ładowanie wykonawców…');
    page.appendChild(status);

    try {
      const params = {
        ParentId: getLibraryId(),
        PersonTypes: 'Actor',
        StartIndex: startIndex,
        Limit: pageSize,
        SortBy: 'SortName',
        SortOrder: 'Ascending'
      };
      if (onlyWithPhoto) {
        params.IsMissingPrimaryImage = 'false';
      }
      if (searchTerm) {
        params.SearchTerm = searchTerm;
      } else if (selectedLetter) {
        params.NameStartsWith = selectedLetter;
      }
      const data = await query('Persons', params);
      if (current !== requestNumber) return;
      status.textContent = `${data.TotalRecordCount || 0} wykonawców`;
      const grid = el('div', 'ah-grid');
      for (const person of data.Items || []) {
        grid.appendChild(performerCard(person, () => showMovies(person)));
      }
      page.appendChild(grid);

      const controls = el('div', 'ah-controls');
      const prev = button('Poprzednia', () => {
        startIndex = Math.max(0, startIndex - pageSize);
        showActors();
        page.scrollTop = 0;
      });
      prev.disabled = startIndex === 0;
      const next = button('Następna', () => {
        startIndex += pageSize;
        showActors();
        page.scrollTop = 0;
      });
      next.disabled = startIndex + pageSize >= (data.TotalRecordCount || 0);
      controls.append(prev, el('span', '', `${startIndex + 1}–${Math.min(startIndex + pageSize, data.TotalRecordCount || 0)}`), next);
      if (data.TotalRecordCount) page.appendChild(controls);
    } catch (err) {
      if (current === requestNumber) error(status, err);
    }
  }

  async function showMovies(person) {
    selectedPerson = person;
    const current = ++requestNumber;
    const page = root('actors');
    const head = el('div', 'ah-head ah-person-head');
    head.appendChild(button('← Wykonawcy', showActors));

    const photoUrl = imageUrl(person, 180);
    if (photoUrl) {
      const avatar = el('img', 'ah-performer-avatar');
      avatar.src = photoUrl;
      avatar.alt = person.Name || '';
      head.appendChild(avatar);
    }

    head.appendChild(el('h2', '', person.Name));
    head.appendChild(button('Profil wykonawcy', () => goToItem(person.Id)));
    page.appendChild(head);
    const status = el('p', '', 'Ładowanie filmów…');
    page.appendChild(status);
    try {
      const data = await query('Items', {
        ParentId: getLibraryId(),
        PersonIds: person.Id,
        Recursive: 'true',
        IncludeItemTypes: 'Movie',
        Limit: 500,
        SortBy: 'SortName',
        SortOrder: 'Ascending',
        Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres,DateCreated'
      });
      if (current !== requestNumber) return;
      const allMovies = data.Items || [];
      const grid = el('div', 'ah-movie-grid');

      function updateGrid(filtered) {
        status.textContent = `${filtered.length} z ${allMovies.length} filmów`;
        grid.replaceChildren();
        for (const movie of filtered) {
          grid.appendChild(movieCard(movie));
        }
      }

      const bar = createFilterAndSortBar(allMovies, updateGrid);
      page.appendChild(bar.toolbar);
      page.appendChild(grid);
      updateGrid(allMovies);
    } catch (err) {
      if (current === requestNumber) error(status, err);
    }
  }

  /* -------------------------------------------------------------
     2. WYTWÓRNIE PRODUKCYJNE (STUDIOS)
     ------------------------------------------------------------- */
  async function showStudios() {
    selectedPerson = null;
    selectedStudio = null;
    selectedChannel = null;
    const current = ++requestNumber;
    const page = root('studios');
    const head = el('div', 'ah-head');
    head.appendChild(button('← Wróć', clearView));
    head.appendChild(el('h2', '', 'Wytwórnie'));
    const input = el('input');
    input.type = 'search';
    input.placeholder = 'Szukaj wytwórni…';
    input.setAttribute('aria-label', 'Szukaj wytwórni');
    input.value = searchTerm;
    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        searchTerm = input.value.trim();
        startIndex = 0;
        showStudios();
      }, 300);
    });
    head.appendChild(input);
    page.appendChild(head);

    const status = el('p', '', 'Ładowanie wytwórni…');
    page.appendChild(status);

    try {
      const params = {
        ParentId: getLibraryId(),
        StartIndex: startIndex,
        Limit: pageSize,
        SortBy: 'SortName',
        SortOrder: 'Ascending'
      };
      if (searchTerm) params.SearchTerm = searchTerm;
      const data = await query('Studios', params);
      if (current !== requestNumber) return;
      status.textContent = `${data.TotalRecordCount || 0} wytwórni`;

      const grid = el('div', 'ah-studio-grid');
      for (const studio of data.Items || []) {
        const studioCard = el('button', 'ah-studio-card');
        studioCard.type = 'button';
        const icon = el('div', 'ah-studio-icon', '🏢');
        const name = el('div', 'ah-studio-name', studio.Name);
        studioCard.append(icon, name);
        studioCard.addEventListener('click', () => showStudioMovies(studio));
        grid.appendChild(studioCard);
      }
      page.appendChild(grid);

      if (data.TotalRecordCount > pageSize) {
        const controls = el('div', 'ah-controls');
        const prev = button('Poprzednia', () => {
          startIndex = Math.max(0, startIndex - pageSize);
          showStudios();
          page.scrollTop = 0;
        });
        prev.disabled = startIndex === 0;
        const next = button('Następna', () => {
          startIndex += pageSize;
          showStudios();
          page.scrollTop = 0;
        });
        next.disabled = startIndex + pageSize >= (data.TotalRecordCount || 0);
        controls.append(prev, el('span', '', `${startIndex + 1}–${Math.min(startIndex + pageSize, data.TotalRecordCount || 0)}`), next);
        page.appendChild(controls);
      }
    } catch (err) {
      if (current === requestNumber) error(status, err);
    }
  }

  async function showStudioMovies(studio) {
    selectedStudio = studio;
    const current = ++requestNumber;
    const page = root('studios');
    const head = el('div', 'ah-head');
    head.appendChild(button('← Wytwórnie', showStudios));
    head.appendChild(el('h2', '', studio.Name));
    head.appendChild(button('Profil wytwórni', () => goToItem(studio.Id)));
    page.appendChild(head);
    const status = el('p', '', 'Ładowanie filmów…');
    page.appendChild(status);
    try {
      const data = await query('Items', {
        ParentId: getLibraryId(),
        StudioIds: studio.Id,
        Recursive: 'true',
        IncludeItemTypes: 'Movie',
        Limit: 500,
        SortBy: 'SortName',
        SortOrder: 'Ascending',
        Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres,DateCreated'
      });
      if (current !== requestNumber) return;
      const allMovies = data.Items || [];
      const grid = el('div', 'ah-movie-grid');

      function updateGrid(filtered) {
        status.textContent = `${filtered.length} z ${allMovies.length} filmów`;
        grid.replaceChildren();
        for (const movie of filtered) {
          grid.appendChild(movieCard(movie));
        }
      }

      const bar = createFilterAndSortBar(allMovies, updateGrid);
      page.appendChild(bar.toolbar);
      page.appendChild(grid);
      updateGrid(allMovies);
    } catch (err) {
      if (current === requestNumber) error(status, err);
    }
  }

  /* -------------------------------------------------------------
     3. KANAŁY TELEGRAM (CHANNELS / SOURCES)
     ------------------------------------------------------------- */
  async function showChannels() {
    selectedPerson = null;
    selectedStudio = null;
    selectedChannel = null;
    const current = ++requestNumber;
    const page = root('channels');
    const head = el('div', 'ah-head');
    head.appendChild(button('← Wróć', clearView));
    head.appendChild(el('h2', '', 'Kanały Telegram'));
    const input = el('input');
    input.type = 'search';
    input.placeholder = 'Szukaj kanału…';
    input.setAttribute('aria-label', 'Szukaj kanału');
    input.value = searchTerm;
    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        searchTerm = input.value.trim().toLowerCase();
        filterChannelsList();
      }, 200);
    });
    head.appendChild(input);
    page.appendChild(head);

    const status = el('p', '', 'Ładowanie kanałów…');
    page.appendChild(status);

    let allChannels = [];
    const grid = el('div', 'ah-studio-grid');
    page.appendChild(grid);

    function filterChannelsList() {
      const filtered = searchTerm
        ? allChannels.filter(c => c.toLowerCase().includes(searchTerm))
        : allChannels;
      status.textContent = `${filtered.length} kanałów`;
      grid.replaceChildren();
      for (const channelName of filtered) {
        const channelCard = el('button', 'ah-studio-card');
        channelCard.type = 'button';
        const icon = el('div', 'ah-studio-icon', '📡');
        const name = el('div', 'ah-studio-name', channelName);
        channelCard.append(icon, name);
        channelCard.addEventListener('click', () => showChannelMovies(channelName));
        grid.appendChild(channelCard);
      }
    }

    try {
      const filters = await query('Items/Filters2', { ParentId: getLibraryId() });
      if (current !== requestNumber) return;
      const rawTags = (filters && filters.Tags) || [];
      const channelSet = new Set();
      for (const t of rawTags) {
        if (typeof t === 'string' && t.startsWith('Kanał: ')) {
          channelSet.add(t.slice(7).trim());
        }
      }
      if (channelSet.size === 0) {
        const fallback = [
          'D - PREMIUM 🔞',
          'Gay Porn Full Videos HD',
          'Lukayan SHOW',
          'ONLYFANS X MEN PREMIUM',
          'Only Guys: The best Gay Bareback Porn ☣️💦',
          'Gymrat KINK: Locker room & Body Worship',
          'PornBestsGay',
          'THE G BUNKER',
          '• NakedSword Studios Media Online ▸ Access ®',
          'Gaycest Archive'
        ];
        fallback.forEach(c => channelSet.add(c));
      }
      allChannels = Array.from(channelSet).sort((a, b) => a.localeCompare(b));
      filterChannelsList();
    } catch (err) {
      if (current === requestNumber) error(status, err);
    }
  }

  async function showChannelMovies(channelName) {
    selectedChannel = channelName;
    const current = ++requestNumber;
    const page = root('channels');
    const head = el('div', 'ah-head');
    head.appendChild(button('← Kanały', () => { searchTerm = ''; showChannels(); }));
    head.appendChild(el('h2', '', channelName));
    page.appendChild(head);
    const status = el('p', '', 'Ładowanie filmów…');
    page.appendChild(status);
    try {
      let data = await query('Items', {
        ParentId: getLibraryId(),
        Tags: 'Kanał: ' + channelName,
        Recursive: 'true',
        IncludeItemTypes: 'Movie',
        Limit: 500,
        SortBy: 'SortName',
        SortOrder: 'Ascending',
        Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres,DateCreated'
      });
      if (!data || !data.TotalRecordCount) {
        data = await query('Items', {
          ParentId: getLibraryId(),
          Tags: channelName,
          Recursive: 'true',
          IncludeItemTypes: 'Movie',
          Limit: 500,
          SortBy: 'SortName',
          SortOrder: 'Ascending',
          Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres,DateCreated'
        });
      }
      if (current !== requestNumber) return;
      const allMovies = data.Items || [];
      const grid = el('div', 'ah-movie-grid');

      function updateGrid(filtered) {
        status.textContent = `${filtered.length} z ${allMovies.length} filmów`;
        grid.replaceChildren();
        for (const movie of filtered) {
          grid.appendChild(movieCard(movie));
        }
      }

      const bar = createFilterAndSortBar(allMovies, updateGrid);
      page.appendChild(bar.toolbar);
      page.appendChild(grid);
      updateGrid(allMovies);
    } catch (err) {
      if (current === requestNumber) error(status, err);
    }
  }

  /* -------------------------------------------------------------
     4. HEADER BUTTONS: Wykonawcy, Wytwórnie, Kanały, 🎲 Losuj
     ------------------------------------------------------------- */
  function attachHeaderButtons() {
    const header = document.querySelector('.skinHeader') || document.querySelector('header');
    if (!header) return;

    const target = header.querySelector('.headerLeft') || header.querySelector('.headerTop') || header;

    if (!document.getElementById('ah-header-actors-btn')) {
      const btn = el('button', 'ah-header-btn', '🎭 Wykonawcy');
      btn.id = 'ah-header-actors-btn';
      btn.type = 'button';
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (view() && activeMode === 'actors') clearView(); else { startIndex = 0; searchTerm = ''; selectedLetter = ''; showActors(); }
      });
      target.appendChild(btn);
    }

    if (!document.getElementById('ah-header-studios-btn')) {
      const btn = el('button', 'ah-header-btn', '🏢 Wytwórnie');
      btn.id = 'ah-header-studios-btn';
      btn.type = 'button';
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (view() && activeMode === 'studios') clearView(); else { startIndex = 0; searchTerm = ''; showStudios(); }
      });
      target.appendChild(btn);
    }

    if (!document.getElementById('ah-header-channels-btn')) {
      const btn = el('button', 'ah-header-btn', '📡 Kanały');
      btn.id = 'ah-header-channels-btn';
      btn.type = 'button';
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (view() && activeMode === 'channels') clearView(); else { startIndex = 0; searchTerm = ''; showChannels(); }
      });
      target.appendChild(btn);
    }

    // Nowy przycisk: 🎲 Losuj film
    if (!document.getElementById('ah-header-random-btn')) {
      const btn = el('button', 'ah-header-btn');
      btn.id = 'ah-header-random-btn';
      btn.type = 'button';
      btn.title = 'Wylosuj losowy film / scenę';
      btn.innerHTML = `${diceSvg} <span>Losuj</span>`;
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        btn.innerHTML = `${spinnerSvg} <span>Losowanie…</span>`;
        try {
          const data = await query('Items', {
            ParentId: getLibraryId(),
            Recursive: 'true',
            IncludeItemTypes: 'Movie',
            SortBy: 'Random',
            Limit: 1
          });
          if (data && data.Items && data.Items.length > 0) {
            goToItem(data.Items[0].Id);
          }
        } catch (err) {
          console.error('[Adult Hub] Random movie error:', err);
        } finally {
          btn.innerHTML = `${diceSvg} <span>Losuj</span>`;
        }
      });
      target.appendChild(btn);
    }
  }

  /* -------------------------------------------------------------
     5. TABS IN LIBRARY VIEW (EMBY SLIDER & MODERN MENU)
     ------------------------------------------------------------- */
  function attachTab() {
    const sliders = document.querySelectorAll('.emby-tabs-slider');
    for (const slider of sliders) {
      const labels = [...slider.querySelectorAll('button, a')].map(n => n.textContent.trim().toLowerCase());
      if (!labels.some(t => t === 'gatunki' || t === 'genres')) continue;
      const template = slider.querySelector('button.emby-tab-button');

      if (!slider.querySelector('#' + actorsTabId)) {
        const tab = template ? template.cloneNode(false) : el('button', 'emby-tab-button');
        tab.id = actorsTabId;
        tab.type = 'button';
        tab.removeAttribute('data-index');
        tab.removeAttribute('aria-selected');
        tab.textContent = 'Wykonawcy';
        tab.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          if (view() && activeMode === 'actors') clearView(); else { startIndex = 0; searchTerm = ''; selectedLetter = ''; showActors(); }
        });
        slider.appendChild(tab);
      }

      if (!slider.querySelector('#' + studiosTabId)) {
        const tab = template ? template.cloneNode(false) : el('button', 'emby-tab-button');
        tab.id = studiosTabId;
        tab.type = 'button';
        tab.removeAttribute('data-index');
        tab.removeAttribute('aria-selected');
        tab.textContent = 'Wytwórnie';
        tab.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          if (view() && activeMode === 'studios') clearView(); else { startIndex = 0; searchTerm = ''; showStudios(); }
        });
        slider.appendChild(tab);
      }

      if (!slider.querySelector('#' + channelsTabId)) {
        const tab = template ? template.cloneNode(false) : el('button', 'emby-tab-button');
        tab.id = channelsTabId;
        tab.type = 'button';
        tab.removeAttribute('data-index');
        tab.removeAttribute('aria-selected');
        tab.textContent = 'Kanały';
        tab.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          if (view() && activeMode === 'channels') clearView(); else { startIndex = 0; searchTerm = ''; showChannels(); }
        });
        slider.appendChild(tab);
      }
    }
  }

  function attachModernMenu() {
    if (!document.getElementById('moviesPage')) return;
    const menu = document.getElementById('library-view-menu');
    if (!menu) return;
    const genre = [...menu.querySelectorAll('li')].find(n => /^(gatunki|genres)$/i.test(n.textContent.trim()));
    if (!genre) return;

    if (!menu.querySelector('#' + actorsTabId)) {
      const item = genre.cloneNode(false);
      item.id = actorsTabId;
      item.textContent = 'Wykonawcy';
      item.removeAttribute('aria-selected');
      item.classList.remove('Mui-selected');
      item.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        startIndex = 0; searchTerm = ''; selectedLetter = '';
        showActors();
      });
      genre.after(item);
    }

    if (!menu.querySelector('#' + studiosTabId)) {
      const actorsItem = menu.querySelector('#' + actorsTabId) || genre;
      const item = genre.cloneNode(false);
      item.id = studiosTabId;
      item.textContent = 'Wytwórnie';
      item.removeAttribute('aria-selected');
      item.classList.remove('Mui-selected');
      item.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        startIndex = 0; searchTerm = '';
        showStudios();
      });
      actorsItem.after(item);
    }

    if (!menu.querySelector('#' + channelsTabId)) {
      const studiosItem = menu.querySelector('#' + studiosTabId) || genre;
      const item = genre.cloneNode(false);
      item.id = channelsTabId;
      item.textContent = 'Kanały';
      item.removeAttribute('aria-selected');
      item.classList.remove('Mui-selected');
      item.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        startIndex = 0; searchTerm = '';
        showChannels();
      });
      studiosItem.after(item);
    }
  }

  /* -------------------------------------------------------------
     6. DETAIL PAGE: Pastylki, Wykonawcy, Podobne Sceny
     ------------------------------------------------------------- */
  const itemTypeCache = new Map();

  async function loadSimilarScenes(page, itemId) {
    if (similarScenesLoading) return;
    similarScenesLoading = true;
    try {
      const client = api();
      const userId = typeof client?.getCurrentUserId === 'function' ? client.getCurrentUserId() : (client?.userId || '');
      const path = userId ? `Users/${userId}/Items` : 'Items';

      const itemData = await query(path, {
        Ids: itemId,
        Fields: 'People,Studios,Genres,Tags'
      });
      const item = (itemData && itemData.Items && itemData.Items[0]) || null;
      if (!item) return;

      if (item.Type) {
        itemTypeCache.set(itemId, item.Type);
      }

      if (item.Type === 'Person') {
        page.classList.add('ah-person-page', 'ah-is-person');
        page.classList.remove('ah-is-movie');
        page.querySelectorAll('.detailImageContainer, .detailImageContainer .card, .detailImageContainer .cardScalable, .detailImageContainer .cardPadder-portrait').forEach(node => {
          node.classList.add('ah-keep-portrait');
        });
        return;
      } else {
        page.classList.add('ah-is-movie');
        page.classList.remove('ah-person-page', 'ah-is-person');
        page.querySelectorAll('.detailImageContainer, .detailImageContainer .card, .detailImageContainer .cardScalable, .detailImageContainer .cardPadder-portrait').forEach(node => {
          node.classList.remove('ah-keep-portrait');
        });
      }

      let similarItems = [];
      let sectionTitleText = '🔥 Polecane sceny';

      // 1. Priorytet: Wiecej z pierwszym wykonawca
      if (item.People && item.People.length > 0) {
        const actor = item.People[0];
        const res = await query(path, {
          ParentId: getLibraryId(),
          PersonIds: actor.Id,
          ExcludeItemIds: itemId,
          Recursive: 'true',
          IncludeItemTypes: 'Movie',
          Limit: 8,
          SortBy: 'Random',
          Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres'
        });
        if (res && res.Items && res.Items.length > 0) {
          similarItems = res.Items;
          sectionTitleText = `🔥 Więcej z: ${actor.Name}`;
        }
      }

      // 2. Jesli malo wynikow: Wiecej z danej wytworni
      if (similarItems.length < 3 && item.Studios && item.Studios.length > 0) {
        const studio = item.Studios[0];
        const res = await query(path, {
          ParentId: getLibraryId(),
          StudioIds: studio.Id,
          ExcludeItemIds: itemId,
          Recursive: 'true',
          IncludeItemTypes: 'Movie',
          Limit: 8,
          SortBy: 'Random',
          Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres'
        });
        if (res && res.Items && res.Items.length > 0) {
          similarItems = res.Items;
          sectionTitleText = `🔥 Więcej z wytwórni: ${studio.Name}`;
        }
      }

      // 3. Jesli malo wynikow: Wiecej z gatunku
      if (similarItems.length < 3 && item.Genres && item.Genres.length > 0) {
        const genre = item.Genres[0];
        const res = await query(path, {
          ParentId: getLibraryId(),
          Genres: genre,
          ExcludeItemIds: itemId,
          Recursive: 'true',
          IncludeItemTypes: 'Movie',
          Limit: 8,
          SortBy: 'Random',
          Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres'
        });
        if (res && res.Items && res.Items.length > 0) {
          similarItems = res.Items;
          sectionTitleText = `🔥 Więcej z kategorii: ${genre}`;
        }
      }

      // 4. Fallback: losowe sceny z biblioteki
      if (similarItems.length === 0) {
        const res = await query(path, {
          ParentId: getLibraryId(),
          ExcludeItemIds: itemId,
          Recursive: 'true',
          IncludeItemTypes: 'Movie',
          Limit: 8,
          SortBy: 'Random',
          Fields: 'People,RunTimeTicks,MediaStreams,UserData,Genres'
        });
        if (res && res.Items && res.Items.length > 0) {
          similarItems = res.Items;
          sectionTitleText = '🔥 Polecane sceny';
        }
      }

      if (similarItems.length === 0) return;

      const currentHash = location.hash || '';
      if (!currentHash.includes(itemId)) return;

      // Usun poprzednia sekcje, jesli byla
      document.getElementById('ah-similar-scenes')?.remove();

      const container = el('div', 'ah-similar-section');
      container.id = 'ah-similar-scenes';
      const heading = el('h2', 'ah-similar-heading', sectionTitleText);
      container.appendChild(heading);

      const grid = el('div', 'ah-movie-grid');
      for (const m of similarItems) {
        grid.appendChild(movieCard(m));
      }
      container.appendChild(grid);

      const castCollapsible = page.querySelector('#castCollapsible');
      const targetParent = page.querySelector('.detailSectionContent') || page.querySelector('.detailPagePrimaryContent');
      if (castCollapsible && castCollapsible.nextSibling) {
        castCollapsible.parentNode.insertBefore(container, castCollapsible.nextSibling);
      } else if (targetParent) {
        targetParent.appendChild(container);
      }
    } catch (err) {
      console.warn('[Adult Hub] Similar scenes error:', err);
    } finally {
      similarScenesLoading = false;
    }
  }

  function enhanceDetailPage() {
    const page = document.getElementById('itemDetailPage');
    if (!page) return;

    const hash = location.hash || '';
    let itemId = null;
    if (hash.includes('/details')) {
      const q = hash.indexOf('?');
      if (q !== -1) {
        const sp = new URLSearchParams(hash.slice(q + 1));
        itemId = sp.get('id');
      }
    }

    // 0. Wykryj typ: Movie vs Person
    let isPerson = page.classList.contains('ah-person-page');
    let isMovie = false;

    if (itemId && itemTypeCache.has(itemId)) {
      const t = itemTypeCache.get(itemId);
      if (t === 'Person') isPerson = true;
      else if (t === 'Movie') isMovie = true;
    } else if (itemId && !itemTypeCache.has(itemId)) {
      query('Items', { Ids: itemId, Fields: 'Type' }).then(res => {
        const item = res?.Items?.[0];
        if (item?.Type) {
          itemTypeCache.set(itemId, item.Type);
          if (item.Type === 'Person') {
            page.classList.add('ah-person-page', 'ah-is-person');
            page.classList.remove('ah-is-movie');
            page.querySelectorAll('.detailImageContainer, .detailImageContainer .card, .detailImageContainer .cardScalable, .detailImageContainer .cardPadder-portrait, .detailImageContainer .cardPadder-mixedPortrait').forEach(node => {
              node.classList.add('ah-keep-portrait');
            });
          }
        }
      }).catch(() => {});
    }

    if (!isPerson && !isMovie) {
      if (selectedPerson && selectedPerson.Id === itemId) {
        isPerson = true;
      } else if (page.querySelector('#itemBirthday:not(:empty)') || page.querySelector('#childrenCollapsible:not(.hide), #listChildrenCollapsible:not(.hide)')) {
        isPerson = true;
      } else if (page.querySelector('.btnPlay:not(.hide)') || page.querySelector('.btnReplay:not(.hide)') || page.querySelector('#castCollapsible:not(.hide)')) {
        isMovie = true;
      }
    }

    if (isPerson) {
      page.classList.add('ah-is-person');
      page.classList.remove('ah-is-movie');
      page.querySelectorAll('.detailImageContainer, .detailImageContainer .card, .detailImageContainer .cardScalable, .detailImageContainer .cardPadder-portrait, .detailImageContainer .cardPadder-mixedPortrait').forEach(node => {
        node.classList.add('ah-keep-portrait');
      });
    } else {
      // Domyślnie na stronie filmu: kadr poziomy 16:9 widescreen
      page.classList.add('ah-is-movie');
      page.classList.remove('ah-is-person');
      page.querySelectorAll('.detailImageContainer, .detailImageContainer .card, .detailImageContainer .cardScalable, .detailImageContainer .cardPadder-portrait, .detailImageContainer .cardPadder-mixedPortrait').forEach(node => {
        node.classList.remove('ah-keep-portrait');
      });
    }

    // 1. Zmien naglowek sekcji obsady na "Wykonawcy"
    const peopleHeader = page.querySelector('#peopleHeader');
    if (peopleHeader && peopleHeader.textContent !== 'Wykonawcy') {
      peopleHeader.textContent = 'Wykonawcy';
    }

    // 2. Przenies sekcje wykonawcow wyzej
    const castCollapsible = page.querySelector('#castCollapsible');
    const targetParent = page.querySelector('.detailSectionContent') || page.querySelector('.detailPagePrimaryContent');
    if (castCollapsible && targetParent && castCollapsible.parentElement !== targetParent) {
      castCollapsible.classList.remove('hide');
      castCollapsible.style.display = 'block';
      castCollapsible.style.marginTop = '24px';
      targetParent.appendChild(castCollapsible);
    }

    // 3. Obsługa nowej struktury Jellyfin 12: .itemDetailsGroup .detailsGroupItem (Gatunki, Wytwórnie)
    page.querySelectorAll('.detailsGroupItem').forEach(group => {
      const label = group.querySelector('.label');
      const labelText = (label?.textContent || '').trim().toLowerCase();
      if (labelText.includes('gatunk') || labelText.includes('genre')) {
        group.classList.add('ah-group-genres');
        group.querySelectorAll('a.button-link').forEach(a => a.classList.add('ah-chip-genre'));
      } else if (labelText.includes('wytwórn') || labelText.includes('studio')) {
        group.classList.add('ah-group-studios');
        group.querySelectorAll('a.button-link').forEach(a => a.classList.add('ah-chip-studio'));
      }
      // Usun przecinki z elementów span
      group.querySelectorAll('.focuscontainer-x span').forEach(span => {
        for (const child of Array.from(span.childNodes)) {
          if (child.nodeType === Node.TEXT_NODE && child.textContent.includes(',')) {
            child.textContent = child.textContent.replace(/,/g, '').trim();
          }
        }
      });
    });

    // 4. Kolorowanie pastylek w .itemTags oraz usunięcie słowa "Znaczniki:"
    const tagsContainer = page.querySelector('.itemTags');
    if (tagsContainer) {
      for (const node of Array.from(tagsContainer.childNodes)) {
        if (node.nodeType === Node.TEXT_NODE) {
          node.remove();
        }
      }
      tagsContainer.querySelectorAll('a').forEach(a => {
        const text = (a.textContent || '').trim();
        if (text.startsWith('Studio:')) {
          a.classList.add('ah-chip-studio');
          a.textContent = text.replace(/^Studio:\s*/, '');
        } else if (text.startsWith('Kanał:')) {
          a.classList.add('ah-chip-channel');
          a.textContent = text.replace(/^Kanał:\s*/, '');
        } else {
          a.classList.add('ah-chip-tag');
        }
      });
    }

    // 5. Usun przecinki ze starych .itemGenres
    page.querySelectorAll('.itemGenres').forEach(container => {
      for (const node of Array.from(container.childNodes)) {
        if (node.nodeType === Node.TEXT_NODE) {
          node.remove();
        }
      }
    });

    // 6. Zaladuj "🔥 Podobne sceny"
    if (itemId && itemId !== lastDetailItemId) {
      lastDetailItemId = itemId;
      loadSimilarScenes(page, itemId);
    }
  }

  /* -------------------------------------------------------------
     7. ENHANCE NATIVE CARDS (Pastylki gatunku i czas trwania na kafelkach)
     ------------------------------------------------------------- */
  let nativeCardBatchTimer = null;
  const pendingCardIds = new Set();
  const cardDataCache = new Map(); // id -> itemData

  function enhanceNativeCards() {
    const cards = document.querySelectorAll('.card[data-id]:not(.ah-card-enhanced)');
    if (!cards.length) return;

    for (const card of cards) {
      // Ignoruj kafelki z naszych customowych zakladek oraz sekcji obsady
      if (card.closest('#' + viewId) || card.closest('#castContent')) {
        card.classList.add('ah-card-enhanced');
        continue;
      }

      const type = card.dataset.type || card.dataset.itemtype;
      if (type === 'Person' || type === 'Actor') {
        card.classList.add('ah-card-enhanced', 'ah-keep-portrait');
        continue;
      }

      const id = card.dataset.id;
      if (!id) {
        card.classList.add('ah-card-enhanced');
        continue;
      }

      if (cardDataCache.has(id)) {
        applyBadgesToCard(card, cardDataCache.get(id));
      } else {
        pendingCardIds.add(id);
      }
    }

    if (pendingCardIds.size > 0 && !nativeCardBatchTimer) {
      nativeCardBatchTimer = setTimeout(processPendingCards, 50);
    }
  }

  function applyBadgesToCard(card, item) {
    card.classList.add('ah-card-enhanced');
    const scalable = card.querySelector('.cardScalable') || card.querySelector('.cardBox') || card;
    if (!scalable) return;

    // Pastylka gatunku (lewy górny róg)
    if (!scalable.querySelector('.ah-badge-genre') && item.Genres && item.Genres.length > 0) {
      const g = el('span', 'ah-badge ah-badge-genre', item.Genres[0]);
      g.title = item.Genres[0];
      scalable.appendChild(g);
    }

    // Pastylka czasu trwania (prawy dolny róg)
    if (!scalable.querySelector('.ah-badge-duration') && item.RunTimeTicks) {
      const dur = formatDuration(item.RunTimeTicks);
      if (dur) {
        const d = el('span', 'ah-badge ah-badge-duration', dur);
        scalable.appendChild(d);
      }
    }
  }

  async function processPendingCards() {
    nativeCardBatchTimer = null;
    const batch = Array.from(pendingCardIds).slice(0, 60);
    for (const id of batch) pendingCardIds.delete(id);
    if (!batch.length) return;

    try {
      const data = await query('Items', {
        Ids: batch.join(','),
        Fields: 'Genres,RunTimeTicks'
      });
      if (data && data.Items) {
        for (const item of data.Items) {
          cardDataCache.set(item.Id, item);
          const matchedCards = document.querySelectorAll(`.card[data-id="${item.Id}"]`);
          matchedCards.forEach(c => applyBadgesToCard(c, item));
        }
      }
    } catch (err) {
      console.warn('[Adult Hub] Failed to fetch card details:', err);
    }

    if (pendingCardIds.size > 0) {
      nativeCardBatchTimer = setTimeout(processPendingCards, 40);
    }
  }

  /* -------------------------------------------------------------
     8. VIDEO PLAYER OSD: Następna losowa scena (🎲)
     ------------------------------------------------------------- */
  function attachVideoOsdButtons() {
    const osd = document.getElementById('videoOsdPage') || document.querySelector('.videoOsdBottom');
    if (!osd) return;

    const buttonsContainer = osd.querySelector('.buttons');
    if (!buttonsContainer) return;

    let btn = document.getElementById('ah-osd-random-btn');
    if (!btn) {
      btn = el('button', 'ah-osd-random-btn paper-icon-button-light autoSize');
      btn.id = 'ah-osd-random-btn';
      btn.type = 'button';
      btn.title = 'Następna losowa scena (🎲)';
      btn.setAttribute('aria-label', 'Następna losowa scena');
      btn.innerHTML = `<span class="xlargePaperIconButton" style="display:flex;align-items:center;justify-content:center;width:100%;height:100%">${diceSvg}</span>`;
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        btn.innerHTML = `<span class="xlargePaperIconButton" style="display:flex;align-items:center;justify-content:center;width:100%;height:100%">${spinnerSvg}</span>`;
        try {
          const libId = getLibraryId();
          const queryParams = {
            Recursive: 'true',
            IncludeItemTypes: 'Movie',
            SortBy: 'Random',
            Limit: 1
          };
          if (libId) queryParams.ParentId = libId;
          const data = await query('Items', queryParams);
          if (data && data.Items && data.Items.length > 0) {
            const nextItem = data.Items[0];
            const pm = await getPlaybackManager();
            if (pm && typeof pm.play === 'function') {
              await pm.play({ items: [nextItem], startPositionTicks: 0 });
            } else {
              console.warn('[Adult Hub] PlaybackManager not found in OSD');
              window.location.hash = `#/playback/video?id=${nextItem.Id}`;
            }
          }
        } catch (err) {
          console.error('[Adult Hub] OSD Random Scene Error:', err);
        } finally {
          btn.innerHTML = `<span class="xlargePaperIconButton" style="display:flex;align-items:center;justify-content:center;width:100%;height:100%">${diceSvg}</span>`;
        }
      });
    }

    // Umieść przycisk po prawej stronie, bezpośrednio obok ikony serduszka (.btnUserRating)
    const ratingBtn = buttonsContainer.querySelector('.btnUserRating');
    if (ratingBtn) {
      if (ratingBtn.nextElementSibling !== btn) {
        ratingBtn.insertAdjacentElement('afterend', btn);
      }
    } else {
      const rightControls = buttonsContainer.querySelector('.btnSubtitles, .btnAudio, .volumeButtons, .btnVideoOsdSettings');
      if (rightControls) {
        if (btn.nextElementSibling !== rightControls) {
          rightControls.parentNode.insertBefore(btn, rightControls);
        }
      } else if (!buttonsContainer.contains(btn)) {
        buttonsContainer.appendChild(btn);
      }
    }
  }

  // Zdarzenia odtwarzacza
  document.addEventListener('playbackstart', () => {
    setTimeout(attachVideoOsdButtons, 100);
    setTimeout(attachVideoOsdButtons, 400);
    setTimeout(attachVideoOsdButtons, 1000);
  });
  document.addEventListener('showvideoosd', attachVideoOsdButtons);
  window.addEventListener('resize', attachVideoOsdButtons);

  document.addEventListener('click', event => {
    if (view() && event.target.closest('.emby-tabs-slider button:not(#' + actorsTabId + '):not(#' + studiosTabId + '):not(#' + channelsTabId + ')')) {
      clearView();
    }
  }, true);

  window.addEventListener('hashchange', () => {
    clearView();
    setTimeout(attachVideoOsdButtons, 200);
  });
  window.addEventListener('popstate', () => {
    clearView();
  });

  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      attachHeaderButtons();
      attachTab();
      attachModernMenu();
      enhanceDetailPage();
      enhanceNativeCards();
      attachVideoOsdButtons();
    });
  }).observe(document.body, { childList: true, subtree: true });

  attachHeaderButtons();
  attachTab();
  attachModernMenu();
  enhanceDetailPage();
  enhanceNativeCards();
  attachVideoOsdButtons();
})();
