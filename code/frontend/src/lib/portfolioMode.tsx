import { isPlayStoreApp } from './playStoreApp'

/**
 * Portfolio demo: the site is public before a business is registered, so it takes no payments.
 * Set PORTFOLIO_MODE=true in the root .env — docker-compose passes it to this build as
 * VITE_PORTFOLIO_MODE and to the API, which refuses orders (services/billing_service.py).
 */
export function isPortfolioMode(): boolean {
  const value = (import.meta.env.VITE_PORTFOLIO_MODE ?? '').trim().toLowerCase()
  return value === '1' || value === 'true' || value === 'yes' || value === 'on'
}

/** No prices, purchase buttons or pricing links: on the portfolio demo, or inside the Play Store app. */
export function pricesHidden(): boolean {
  return isPortfolioMode() || isPlayStoreApp()
}

export const PORTFOLIO_BANNER_TEXT = '포트폴리오 데모 · 결제 없이 무료로 써 볼 수 있어요'

export function PortfolioBanner() {
  if (!isPortfolioMode()) {
    return null
  }
  return (
    <p className="portfolio-banner" role="note" data-testid="portfolio-banner">
      {PORTFOLIO_BANNER_TEXT}
    </p>
  )
}
