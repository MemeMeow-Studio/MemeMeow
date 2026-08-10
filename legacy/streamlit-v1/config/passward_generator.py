import streamlit_authenticator as stauth
import yaml
import uuid

# 生成密码哈希
passwords = [str(uuid.uuid4())]  # 原始密码
print(f"原始密码: {passwords[0]}")
hashed_passwords = stauth.Hasher(passwords).generate()
print(f"哈希密码: {hashed_passwords[0]}")

yaml_file = f"""auth_enabled: true
credentials:
  usernames:
    admin:
      email: admin@example.com
      name: admin
      password: {hashed_passwords[0]}
cookie:
  expiry_days: 30
  key: mememeow_auth_key
  name: mememeow_auth_cookie
preauthorized:
  emails: []

"""
save_path = os.path.join(os.path.dirname(__file__), 'auth_config.yaml')
with open(save_path, 'w', encoding='utf-8') as f:
    f.write(yaml_file)
print(f"配置文件已保存到 {save_path}")