import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const userAuth = read("../src/pages/UserAuthPage.jsx");
const staffAuth = read("../src/pages/AdminLoginPage.jsx");
const authField = read("../src/components/auth/AuthField.jsx");
const turnstile = read("../src/components/auth/TurnstileWidget.jsx");
const modeTransition = read("../src/components/auth/AuthModeTransition.jsx");
const decorations = read("../src/components/auth/AuthDecorations.jsx");
const styles = read("../src/styles.css");

test("ordinary login keeps the password security contract and controls", () => {
  assert.match(userAuth, /mode === "login"/);
  assert.match(userAuth, /label="密码 PASSWORD" type="password"/);
  assert.match(userAuth, /autoComplete="current-password"/);
  assert.match(userAuth, /showPasswordToggle/);
  assert.match(userAuth, /forgot-password/);
  assert.match(userAuth, /TurnstileWidget/);
  assert.match(userAuth, /authApi\.login\(form\.account\.trim\(\), form\.password, turnstileToken\)/);
});

test("password visibility control is keyboard and screen-reader accessible", () => {
  assert.match(authField, /aria-label=\{passwordVisible \? "隐藏密码" : "显示密码"\}/);
  assert.match(authField, /aria-pressed=\{passwordVisible\}/);
  assert.match(authField, /type="button"/);
  assert.match(authField, /eye-slash/);
  assert.match(styles, /\.auth-field__password-toggle:focus-visible/);
  assert.match(styles, /\.auth-field__control\.has-password-toggle input/);
});

test("auth field leading icons stay above transformed focused inputs", () => {
  assert.match(styles, /\.auth-field__control\s*>\s*svg\s*\{[^}]*z-index:\s*1/);
});

test("user and staff Turnstile integrations use explicit official themes and stable slots", () => {
  assert.match(userAuth, /TurnstileWidget[^\n]*variant="user"[^\n]*size="normal"[^\n]*mountDelay=\{900\}/);
  assert.match(staffAuth, /TurnstileWidget[^\n]*variant="staff"[^\n]*size="flexible"[^\n]*mountDelay=\{900\}/);
  assert.match(turnstile, /theme = variant === "staff" \? "dark" : "light"/);
  assert.match(turnstile, /theme,/);
  assert.match(turnstile, /size,/);
  assert.match(turnstile, /setTimeout\(\(\) =>/);
  assert.match(turnstile, /turnstileScriptPromise/);
  assert.match(turnstile, /turnstile\.remove\?\./);
  assert.match(styles, /\.turnstile-widget__slot \{/);
  assert.doesNotMatch(styles, /turnstile-widget[^\n]*transform:\s*scale/);
});

test("auth entrance motion restores a restrained opacity and transform hierarchy", () => {
  assert.doesNotMatch(userAuth, /scale:\s*0\.1|elastic\.out|stagger:\s*0\.1/);
  assert.match(userAuth, /scale:\s*\.94/);
  assert.match(userAuth, /stagger:\s*\.055/);
  assert.doesNotMatch(staffAuth, /scale:\s*0\.72|stagger:\s*0\.07/);
  assert.match(staffAuth, /scale:\s*\.96/);
  assert.match(modeTransition, /querySelectorAll\("\[data-auth-step\]"\)/);
  assert.match(modeTransition, /scale:\s*\.985/);
  assert.match(modeTransition, /stagger:\s*\.045/);
  assert.doesNotMatch(modeTransition, /scale:\s*0\.88/);
  assert.doesNotMatch(decorations, /from "gsap"|gsap\./);
  assert.match(styles, /\.auth-triangle-rotor\s*\{[^}]*animation:\s*auth-triangle-drift/);
  assert.match(styles, /@keyframes auth-triangle-drift/);
  assert.doesNotMatch(styles, /transition:\s*all/);

  for (const source of [userAuth, staffAuth, modeTransition]) {
    assert.doesNotMatch(source, /(?:^|[,{}]\s*)(?:width|height|top|left|margin|padding|borderWidth):/m);
  }
});

test("staff auth preserves the existing security and navigation surface", () => {
  assert.match(staffAuth, /label="管理员账号"/);
  assert.match(staffAuth, /label="安全口令" type="password"/);
  assert.match(staffAuth, /two_factor_required/);
  assert.match(staffAuth, /进入管理控制室/);
  assert.match(staffAuth, /返回普通用户登录/);
  assert.match(staffAuth, /authApi\.staffLogin\(/);
});
