// @ts-check
/**
 * Enterprise blueprint: playwright-js-v1 — shared page-object plumbing.
 * Playwright auto-waits on every locator action, so this base intentionally
 * holds only the patterns the legacy estate genuinely needed beyond that.
 */

/** Escape a literal string for use inside a RegExp. */
export function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Whole-text, case-insensitive matcher (legacy suites compared labels case-insensitively). */
export function exactTextCi(text) {
  return new RegExp(`^\\s*${escapeRegExp(text)}\\s*$`, 'i');
}

export class BasePage {
  /** @param {import('@playwright/test').Page} page */
  constructor(page) {
    this.page = page;
  }

  /**
   * Blueprint pattern "poll-populated-select": some dropdowns render
   * placeholder-only first and populate their real <option>s a moment later
   * via AJAX, so wait for the target option to exist before selecting.
   * @param {import('@playwright/test').Locator} selectLocator
   * @param {string} label visible option text
   */
  async selectOptionWhenPopulated(selectLocator, label) {
    await selectLocator
      .locator('option')
      .filter({ hasText: exactTextCi(label) })
      .first()
      .waitFor({ state: 'attached' });
    await selectLocator.selectOption({ label });
  }

  /**
   * Legacy parity with BasePage.getText: wait for visibility, then read and
   * trim. (textContent alone only waits for attachment, and SPA views can be
   * attached-but-empty during hash-route transitions.)
   * @param {import('@playwright/test').Locator} locator
   * @returns {Promise<string>}
   */
  async readText(locator) {
    await locator.first().waitFor({ state: 'visible' });
    return ((await locator.first().textContent()) ?? '').trim();
  }

  /**
   * Blueprint pattern "conditional-dismiss" support: bounded visibility probe
   * for elements that may legitimately never appear.
   * @param {import('@playwright/test').Locator} locator
   * @param {number} timeoutMs
   * @returns {Promise<boolean>}
   */
  async isVisibleWithin(locator, timeoutMs) {
    try {
      await locator.first().waitFor({ state: 'visible', timeout: timeoutMs });
      return true;
    } catch {
      return false;
    }
  }
}
