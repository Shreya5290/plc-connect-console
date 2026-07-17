import re

from django import forms


IPV4_PATTERN = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
HOST_PATTERN = re.compile(r'^[A-Za-z0-9.\-]+$')
CONFIG_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_.\- ]+$')


class PlcReadForm(forms.Form):
    word_swapped = forms.ChoiceField(
        label='Word Swapped',
        choices=(('no', 'No'), ('yes', 'Yes')),
        required=False,
        initial='no',
        widget=forms.Select(attrs={'id': 'word_swapped'}),
    )
    plc_brand = forms.ChoiceField(
        label='PLC Brand',
        choices=(
            ('allen_bradley', 'Allen-Bradley'),
            ('siemens_snap7', 'Siemens (Snap7)'),
            ('pymodbus', 'Modbus'),
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

    siemens_operation = forms.ChoiceField(
        label='Operation',
        choices=(('read', 'Read'), ('write', 'Write')),
        required=False,
        initial='read',
        widget=forms.Select(attrs={'id': 'siemens_operation'}),
    )
    siemens_write_value = forms.CharField(
        label='Write Value',
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={
                'id': 'siemens_write_value',
                'placeholder': 'Value to write',
                'autocomplete': 'off',
            }
        ),
    )
    plc_port = forms.IntegerField(
        label='Port',
        required=False,
        min_value=1,
        max_value=65535,
        widget=forms.NumberInput(attrs={'id': 'plc_port', 'placeholder': 'Port', 'autocomplete': 'off'}),
    )
    modbus_function = forms.ChoiceField(
        label='Function',
        choices=(
            ('holding', 'Holding Register'),
            ('input_register', 'Input Register'),
            ('coil', 'Coil'),
            ('discrete_input', 'Discrete Input'),
        ),
        required=False,
        initial='holding',
        widget=forms.Select(attrs={'id': 'modbus_function'}),
    )
    modbus_operation = forms.ChoiceField(
        label='Operation',
        choices=(('read', 'Read'), ('write', 'Write')),
        required=False,
        initial='read',
        widget=forms.Select(attrs={'id': 'modbus_operation'}),
    )
    modbus_data_type = forms.ChoiceField(
        label='Data Type',
        choices=(
            ('uint16', 'UInt16'),
            ('int16', 'Int16'),
            ('uint32', 'UInt32'),
            ('int32', 'Int32'),
            ('uint64', 'UInt64'),
            ('int64', 'Int64'),
            ('float32', 'Float32'),
            ('float64', 'Float64'),
            ('bool', 'Boolean'),
            ('string', 'String'),
        ),
        required=False,
        initial='uint16',
        widget=forms.Select(attrs={'id': 'modbus_data_type'}),
    )
    modbus_write_value = forms.CharField(
        label='Write Value',
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={
                'id': 'modbus_write_value',
                'placeholder': 'Value to write',
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
        brand = cleaned.get('plc_brand', 'allen_bradley')
        slot = cleaned.get('plc_slot')
        if slot in (None, ''):
            slot = 0
        cleaned['plc_slot'] = slot
        if ip:
            cleaned['plc_ip'] = f'{ip}/{slot}'
        # Siemens rack and slot are always 0
        cleaned['plc_rack'] = 0
        # Ensure defaults for Modbus fields
        port = cleaned.get('plc_port')
        if port in (None, ''):
            cleaned['plc_port'] = None
        operation = cleaned.get('modbus_operation') or 'read'
        if cleaned.get('plc_brand') == 'pymodbus' and operation == 'write' and not cleaned.get('modbus_write_value', '').strip():
            self.add_error('modbus_write_value', 'Enter a value to write.')
        # Siemens write validation
        siemens_op = cleaned.get('siemens_operation') or 'read'
        if brand == 'siemens_snap7' and siemens_op == 'write' and not cleaned.get('siemens_write_value', '').strip():
            self.add_error('siemens_write_value', 'Enter a value to write.')
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
    max_tags = forms.IntegerField(
        label='Max Tags to Discover',
        required=False,
        min_value=1,
        max_value=10000,
        initial=1000,
        widget=forms.NumberInput(
            attrs={
                'id': 'max_tags',
                'placeholder': '1000',
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
        max_tags = cleaned.get('max_tags')
        if max_tags in (None, ''):
            max_tags = 1000
        cleaned['max_tags'] = max_tags
        return cleaned


class ClearHistoryForm(forms.Form):
    protocol = forms.ChoiceField(choices=(('plc', 'PLC'), ('opcua', 'OPC UA')))


class PlcConfigSaveForm(forms.Form):
    config_name = forms.CharField(
        label='Configuration Name',
        max_length=80,
        required=True,
        widget=forms.TextInput(
            attrs={
                'id': 'config_name',
                'placeholder': 'line-1-shift-a',
                'autocomplete': 'off',
            }
        ),
    )

    def clean_config_name(self):
        value = self.cleaned_data['config_name'].strip()
        if not value:
            raise forms.ValidationError('Enter a configuration file name.')
        if not CONFIG_NAME_PATTERN.match(value):
            raise forms.ValidationError('Use letters, numbers, space, dash, underscore, or dot only.')
        return value


class PlcConfigLoadForm(forms.Form):
    config_file = forms.ChoiceField(label='Saved Configurations', choices=(), required=True)

    def __init__(self, *args, **kwargs):
        file_choices = kwargs.pop('file_choices', ())
        super().__init__(*args, **kwargs)
        self.fields['config_file'].choices = file_choices or (('', 'No saved Config files found'),)
        self.fields['config_file'].widget.attrs.update({'id': 'config_file'})


class PlcConfigImportForm(forms.Form):
    config_upload = forms.FileField(label='Import Config File', required=True)

    def clean_config_upload(self):
        file_obj = self.cleaned_data['config_upload']
        name = (getattr(file_obj, 'name', '') or '').lower()
        if not (name.endswith('.yaml') or name.endswith('.yml')):
            raise forms.ValidationError('Upload a .yaml or .yml file.')
        return file_obj
