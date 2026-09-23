"""Bounded real interactions with generated local pages in a network-free browser."""
from pathlib import Path


def controls_snapshot(page):
    """Describe current visible controls so a reviewer can propose actual actions."""
    return page.locator('input:visible, select:visible, textarea:visible, button:visible, [role="button"]:visible').evaluate_all('''elements => {
      const selector = el => {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        while (el && el.tagName.toLowerCase() !== 'html') {
          const tag = el.tagName.toLowerCase();
          const siblings = el.parentElement ? [...el.parentElement.children].filter(x => x.tagName === el.tagName) : [el];
          parts.unshift(tag + ':nth-of-type(' + (siblings.indexOf(el) + 1) + ')');
          el = el.parentElement;
        }
        return parts.join(' > ');
      };
      return elements.map(el => ({
        selector: selector(el), tag: el.tagName.toLowerCase(), type: el.type || el.getAttribute('role') || '',
        id: el.id || '', name: el.name || '',
        label: el.getAttribute('aria-label') || [...(el.labels || [])].map(x => x.innerText.trim()).join(' '),
        text: el.innerText || '', placeholder: el.getAttribute('placeholder') || '',
        value: el.value === undefined ? '' : String(el.value), disabled: Boolean(el.disabled),
        checked: Boolean(el.checked),
        options: el.options ? [...el.options].map(o => ({value:o.value, label:o.label, selected:o.selected})) : []
      }));
    }''')


def run_interactions(html_path, actions, directory=None):
    """Run up to ten click/fill/select/check/assert_text steps in a fresh context.

    Actions contain ``action``, ``selector`` and, where relevant, ``value``.
    Check accepts a Boolean value (default True). Assert_text checks visible text
    on exactly one matched element. The first failed step stops this plan.
    """
    from playwright.sync_api import sync_playwright

    allowed = {'click', 'fill', 'select', 'check', 'assert_text'}
    if len(actions) > 10:
        raise ValueError('interaction plan exceeds ten steps')
    for action in actions:
        if action.get('action') not in allowed:
            raise ValueError('unsupported browser action')
        if not isinstance(action.get('selector'), str) or not action['selector']:
            raise ValueError('browser action requires a selector')
        if action['action'] in {'fill', 'select', 'assert_text'} and not isinstance(action.get('value'), str):
            raise ValueError('browser action requires a text value')
        if action['action'] == 'check' and not isinstance(action.get('value', True), bool):
            raise ValueError('check requires a Boolean value')
    errors = []
    steps = []
    screenshots = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width':1440,'height':1000},
                                          accept_downloads=False, service_workers='block')
            context.route('**/*', lambda route: route.abort())
            page = context.new_page()
            page.set_default_timeout(2500)
            page.on('pageerror', lambda error: errors.append(str(error)))
            csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
            page.set_content('<meta http-equiv="Content-Security-Policy" content="' + csp + '">' +
                             Path(html_path).read_text(encoding='utf-8'), wait_until='load')
            controls = controls_snapshot(page)
            initial_text = page.locator('body').inner_text()
            for index, action in enumerate(actions):
                step = {'index':index, 'action':dict(action), 'passed':False}
                try:
                    locator = page.locator(action['selector'])
                    if locator.count() != 1:
                        raise ValueError('action selector must match exactly one element')
                    if not locator.is_visible():
                        raise ValueError('action target is not visible')
                    step['before_text'] = page.locator('body').inner_text()
                    operation = action['action']
                    if operation == 'click':
                        locator.click()
                    elif operation == 'fill':
                        locator.fill(action['value'])
                    elif operation == 'select':
                        locator.select_option(value=action['value'])
                    elif operation == 'check':
                        locator.set_checked(action.get('value', True))
                    elif operation == 'assert_text':
                        # Playwright waits for asynchronous local UI updates.
                        from playwright.sync_api import expect
                        expect(locator).to_contain_text(action['value'], use_inner_text=True, timeout=2500)
                        step['observed_text'] = locator.inner_text()
                    step['after_text'] = page.locator('body').inner_text()
                    step['controls_after'] = controls_snapshot(page)
                    step['passed'] = True
                except Exception as error:
                    step['error'] = str(error)
                steps.append(step)
                if not step['passed']:
                    break
            final_text = page.locator('body').inner_text()
            if directory is not None:
                target = Path(directory)
                target.mkdir(parents=True, exist_ok=True)
                screenshot = target / 'interactions-final.png'
                page.screenshot(path=str(screenshot), full_page=True)
                screenshots.append(str(screenshot))
            return {'passed':len(steps) == len(actions) and all(s['passed'] for s in steps) and not errors,
                    'steps':steps, 'errors':errors, 'controls':controls, 'initial_text':initial_text,
                    'final_text':final_text, 'screenshots':screenshots,
                    'evidence_kind':'TOOL_RESULT', 'human_effect':'AWAITING_HUMAN_EVIDENCE'}
        finally:
            browser.close()
