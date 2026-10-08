import { BackendStatus } from "./BackendStatus";
import { UserMenu } from "./UserMenu";
import styles from "./TopBar.module.css";

export function TopBar() {
  return (
    <header className={styles.topbar}>
      <BackendStatus />
      <UserMenu />
    </header>
  );
}
