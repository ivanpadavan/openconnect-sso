(() => {
    const ruleSets = [];

    window.__openconnect_sso_register_rules = (rules) => {
        ruleSets.push({rules, stopped: false});
    };

    window.__openconnect_sso_autofill = (remaining) => {
        let performed = 0;
        for (const ruleSet of ruleSets) {
            if (ruleSet.stopped) {
                continue;
            }
            try {
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
                    if (performed === remaining) {
                        return {performed, overflow: true};
                    }

                    elem.dispatchEvent(new Event("focus"));
                    if (!elem.isConnected) {
                        continue;
                    }
                    if (click) {
                        if (!elem.matches(":disabled")) {
                            elem.click();
                            performed++;
                        }
                        continue;
                    }
                    const previous = elem.value;
                    elem.value = value;
                    if (elem.value === previous) {
                        continue;
                    }
                    performed++;
                    if (sms) {
                        elem.dispatchEvent(new Event("input"));
                        elem.dispatchEvent(new Event("change"));
                        elem.dispatchEvent(new Event("blur"));
                    } else {
                        elem.dispatchEvent(new Event("blur"));
                        elem.dispatchEvent(new Event("input"));
                    }
                }
            } catch (error) {
                ruleSet.stopped = true;
                console.warn("SSO autofill rule failed", error);
            }
        }
        return {performed, overflow: false};
    };
})();
