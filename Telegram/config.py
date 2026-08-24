import yaml

with open('config.yml','r',encoding='utf-8') as f:
    set_yaml = yaml.safe_load(f)



bot_token =  set_yaml['settings']['bot_token']
admin_ids = set_yaml['settings']['admin_ids']
whatsapp_service = set_yaml['settings']['whatsapp_service']
API_ID = set_yaml['settings']['API_ID']
API_HASH = set_yaml['settings']['API_HASH']