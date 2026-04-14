import re

from django import forms


IPV4_PATTERN = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
HOST_PATTERN = re.compile(r'^[A-Za-z0-9.\-]+$')


class PlcReadForm(forms.Form):
    plc_brand = forms.ChoiceField(
        label='PLC Brand',
        choices=(
            ('allen_bradley', 'Allen-Bradley'),
            ('siemens', 'Siemens'),
            ('modbus', 'Modbus'),
        ),
        required=True,
        initial='allen_bradley',
        widget=forms.Select(attrs={'id': 'plc_brand'}),
    )
    plc_ip_address = forms.CharField(
        label='PLC IP Address',
        max_length=64,
        required=True,
        widget=forms.TextInput(
            attrs={
                'id': 'plc_ip_address',
                'placeholder': '10.191.175.15',
                'autocomplete': 'off',
            }
        ),
    )
    plc_slot = forms.IntegerField(
        label='Slot',
        required=False,
        min_value=0,
        max_value=99,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                'id': 'plc_slot',
                'placeholder': '0',
                'autocomplete': 'off',
            }
        ),
    )
    plc_tag = forms.CharField(
        label='PLC Variable',
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={
                'id': 'plc_tag',
                'placeholder': 'Enter variable name (e.g., AI[0].Val)',
                'autocomplete': 'off',
            }
        ),
    )

    def clean_plc_ip_address(self):
        value = self.cleaned_data['plc_ip_address'].strip()
        if not IPV4_PATTERN.match(value):
            raise forms.ValidationError('Enter a valid PLC IP address in this format: 10.191.175.15')

        parts = value.split('.')
        if any(int(part) > 255 for part in parts):
            raise forms.ValidationError('Each IP segment must be between 0 and 255.')
        return value

    def clean(self):
        cleaned = super().clean()
        ip = cleaned.get('plc_ip_address', '').strip()
        slot = cleaned.get('plc_slot')
        if slot in (None, ''):
            slot = 0
        cleaned['plc_slot'] = slot
        if ip:
            cleaned['plc_ip'] = f'{ip}/{slot}'
        return cleaned


class OpcUaFetchForm(forms.Form):
    opcua_host = forms.CharField(
        label='OPC UA Host/IP',
        max_length=120,
        required=True,
        widget=forms.TextInput(
            attrs={
                'id': 'opcua_host',
                'placeholder': '10.191.175.15',
                'autocomplete': 'off',
            }
        ),
    )
    opcua_port = forms.IntegerField(
        label='Port',
        required=False,
        min_value=1,
        max_value=65535,
        initial=4840,
        widget=forms.NumberInput(
            attrs={
                'id': 'opcua_port',
                'placeholder': '4840',
                'autocomplete': 'off',
            }
        ),
    )

    def clean_opcua_host(self):
        value = self.cleaned_data['opcua_host'].strip()
        if not value:
            raise forms.ValidationError('Enter a valid OPC UA host or IP address.')
        if not HOST_PATTERN.match(value):
            raise forms.ValidationError('Enter a valid OPC UA host or IP address.')
        if IPV4_PATTERN.match(value):
            parts = value.split('.')
            if any(int(part) > 255 for part in parts):
                raise forms.ValidationError('Each IP segment must be between 0 and 255.')
        return value

    def clean(self):
        cleaned = super().clean()
        host = cleaned.get('opcua_host', '').strip()
        port = cleaned.get('opcua_port')
        if port in (None, ''):
            port = 4840
        cleaned['opcua_port'] = port
        if host:
            cleaned['opcua_endpoint'] = f'{host}:{port}'
        return cleaned


class ClearHistoryForm(forms.Form):
    protocol = forms.ChoiceField(choices=(('plc', 'PLC'), ('opcua', 'OPC UA')))
