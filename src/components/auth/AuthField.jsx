import { useId, useState } from "react";
import { Icon } from "../Icon.jsx";

export function AuthField({ inputRef, icon, label, showPasswordToggle = false, ...inputProps }) {
  const generatedId = useId();
  const inputId = inputProps.id || `auth-field-${generatedId.replace(/:/g, "")}`;
  const canTogglePassword = showPasswordToggle && inputProps.type === "password";
  const [passwordVisible, setPasswordVisible] = useState(false);
  const inputType = canTogglePassword && passwordVisible ? "text" : inputProps.type;

  return (
    <label className="auth-field" data-auth-step htmlFor={inputId}>
      <span>{label}</span>
      <div className={canTogglePassword ? "auth-field__control has-password-toggle" : "auth-field__control"}>
        <Icon name={icon} aria-hidden="true" />
        <input ref={inputRef} id={inputId} {...inputProps} type={inputType} />
        {canTogglePassword && (
          <button
            className="auth-field__password-toggle"
            type="button"
            aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
            aria-pressed={passwordVisible}
            onClick={() => setPasswordVisible((visible) => !visible)}
          >
            <Icon name={passwordVisible ? "eye-slash" : "eye"} aria-hidden="true" />
          </button>
        )}
      </div>
    </label>
  );
}
