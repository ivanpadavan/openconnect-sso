(() => {
    const ruleSets = [];

    window.__openconnect_sso_register_rules = (rules) => {
        ruleSets.push({rules, stopped: false});
        if (ruleSets.length === 1) {
            new QWebChannel(qt.webChannelTransport, ({objects}) => autoFill(objects.budget));
        }
    };

    async function autoFill(budget) {
        if (budget.exhausted) {
            return;
        }

        for (const ruleSet of ruleSets) {
            if (ruleSet.stopped) {
                continue;
            }
            for (const rule of ruleSet.rules) {
                const elem = document.querySelector(rule.selector);
                if (!elem) {
                    continue;
                }
                if (rule.type === "stop") {
                    ruleSet.stopped = true;
                    break;
                }

                const click = rule.type === "click";
                const sms = rule.type === "sms";
                const value = sms ? window.__openconnect_sso_sms : rule.value;
                if (click ? elem.matches(":disabled") : !value || elem.value === value) {
                    continue;
                }

                // Wait for Qt to reserve an action before changing the page.
                // Every document uses the same budget, including popup documents.
                const allowed = await new Promise((resolve) => budget.reserve(resolve));
                if (!allowed) {
                    return;
                }
                if (!elem.isConnected) {
                    continue;
                }
                elem.dispatchEvent(new Event("focus"));
                if (click) {
                    elem.click();
                } else {
                    elem.value = value;
                    if (sms) {
                        elem.dispatchEvent(new Event("input"));
                        elem.dispatchEvent(new Event("change"));
                        elem.dispatchEvent(new Event("blur"));
                    } else {
                        elem.dispatchEvent(new Event("blur"));
                        elem.dispatchEvent(new Event("input"));
                    }
                }
            }
        }

        if (!budget.exhausted && ruleSets.some((ruleSet) => !ruleSet.stopped)) {
            setTimeout(() => autoFill(budget), 1000);
        }
    }
})();
