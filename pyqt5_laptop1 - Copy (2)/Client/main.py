import sys
import requests
from PyQt5.QtWidgets import QApplication, QMessageBox
from Client.Login import Login
from Client.client_config import SERVER_URL
from Client import Client_crypto as crypto_utils


def main():
    app = QApplication(sys.argv)

    try:
        r = requests.get(f"{SERVER_URL}/public_key", timeout=15)
        pem = r.json()["public_key"]
        public_key = crypto_utils.import_public_key(pem)

        aes_key = crypto_utils.generate_aes_key()
        encrypted_key = crypto_utils.rsa_encrypt(public_key, aes_key)

        r2 = requests.post(f"{SERVER_URL}/session_key",
                           json={"encrypted_key": encrypted_key}, timeout=15)
        session_token = r2.json()["session_token"]

        crypto_utils.AES_KEY = aes_key
        crypto_utils.SESSION_TOKEN = session_token
        crypto_utils.SERVER_PUBLIC_KEY = public_key
        print("Encrypted session established with server")

    except requests.exceptions.ConnectionError:
        QMessageBox.critical(None, "Connection Error",
                             f"Cannot connect to server at {SERVER_URL}.\n\nMake sure the server is running.")
        sys.exit(1)
    except requests.exceptions.ReadTimeout:
        QMessageBox.critical(None, "Connection Timeout",
                             f"Server at {SERVER_URL} did not respond in time.\n\nCheck that the server is running and the IP is correct.")
        sys.exit(1)
    except Exception as e:
        QMessageBox.critical(None, "Startup Error", f"Failed to connect to server:\n{e}")
        sys.exit(1)

    login_window = Login()
    login_window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
