#! coding: utf-8

import os
import json
import logging
import tempfile
import requests
import sqlalchemy
import shutil
from abc import ABCMeta, abstractmethod
from routes import url_for
from pylons.config import config
import ckan
import ckan.lib.search
import ckan.model as model
import ckan.tests.helpers as helpers
import ckan.tests.factories as factories
from ckanext.gobar_theme.lib.datajson_actions import CACHE_DIRECTORY, CACHE_FILENAME
from ckanext.gobar_theme.lib.datajson_actions import generate_new_cache_file
from ckanext.gobar_theme.config_controller import GobArConfigController

logger = logging.getLogger(__name__)
submit_and_follow = helpers.submit_and_follow


class GobArConfigControllerForTest(GobArConfigController):
    CONFIG_PATH = CACHE_DIRECTORY + "test_settings.json"


class TestAndino(helpers.FunctionalTestBase):
    __metaclass__ = ABCMeta

    @abstractmethod
    def __init__(self):
        self.app = self._get_test_app()
        self.org = None

    def setup(self):
        super(TestAndino, self).setup()
        self.org = factories.Organization()

    @classmethod
    def setup_class(cls):

        def search_for_file(filename):
            for root, dirs, files in os.walk("/"):
                for name in files:
                    if filename == name:
                        return os.path.join(root, name)

        super(TestAndino, cls).setup_class()
        # Si existe, cambiamos el nombre de la caché con los datos del nodo, para poder usar una caché de testeo
        try:
            os.rename(CACHE_FILENAME, CACHE_DIRECTORY + "datajson_cache_backup.json")
        except OSError:
            # No existe, por lo que no hay nada qué renombrar
            pass
        # Creo un nuevo settings.json para ser usado durante el testeo
        settings_path = os.path.relpath(os.path.dirname('/tests_config/test_settings.json'))
        data = requests.get('https://raw.githubusercontent.com/datosgobar/portal-base/master/'
                            'base_portal/roles/portal/templates/ckan/default.json.j2')
        with open(settings_path, "w") as file:
            file.write(data.text)

    @classmethod
    def teardown_class(cls):
        super(TestAndino, cls).teardown_class()
        model.repo.rebuild_db()
        ckan.lib.search.clear_all()
        try:
            os.remove(CACHE_FILENAME)
        except OSError:
            # No se creó una caché de testeo
            pass
        os.rename(CACHE_DIRECTORY + "datajson_cache_backup.json", CACHE_FILENAME)

    def create_package_with_n_resources(self, n=0, data_dict={}):
        '''
        :param n: cantidad de recursos a crear (ninguno por default)
        :param data_dict: campos opcionales pertenecientes al dataset cuyos datos se quieren proveer (no utilizar campos
            cuyos valores contengan tildes u otros caracteres que provoquen errores por UnicodeDecode.)
        :return: dataset con n recursos
        '''
        resources_list = []
        for i in range(n):
            resources_list.append({'url': 'http://test.com/', 'custom_resource_text': 'my custom resource #%d' % i})
        data_dict['resources'] = resources_list
        if 'name' not in data_dict.keys():
            data_dict['name'] = 'test_package'
        return helpers.call_action('package_create', **data_dict)

    # ------ Methods with factories ------ #

    def get_page_response(self, url, admin_required=False):
        '''
        :param url: url a la cual se deberá acceder
        :param admin_required: deberá ser True en caso de que se quiera realizar una operación que necesite un admin
        :return: env relacionado al usuario utilizado, y el response correspondiente a la url recibida
        '''
        if admin_required:
            user = factories.Sysadmin()
        else:
            user = factories.User()
            # Los usuarios colaboradores requieren una organización para manipular datasets
            # org = factories.Organization()
            self.org['users'].append(user)
        env = {'REMOTE_USER': user['name'].encode('ascii')}
        page_url = url_for(url)
        response = self.app.get(url=page_url, extra_environ=env)
        if '30' in response.status:
            # Hubo una redirección; necesitamos la URL final para obtener sus forms
            response = self.app.get(url=url(response.location), extra_environ=env)
        return env, response

    # --- Datasets --- #

    def create_package_with_one_resource_using_forms(self, dataset_name=u'package-with-one-resource',
                                                     resource_url=u'http://example.com/resource'):
        env, response = self.get_page_response('/dataset/new')
        form = response.forms['dataset-edit']
        form['name'] = dataset_name
        response = submit_and_follow(self.app, form, env, 'save', 'continue')

        form = response.forms['resource-edit']
        form['url'] = resource_url
        submit_and_follow(self.app, form, env, 'save', 'go-metadata')
        return model.Package.by_name(dataset_name)

    def update_package_using_forms(self, dataset_name, data_dict={}):
        env, response = self.get_page_response('/dataset/edit/{0}'.format(dataset_name))
        form = response.forms['dataset-edit']
        form['notes'] = u'New description'
        for key, value in data_dict:
            try:
                form[key] = value
            except KeyError:
                logger.warning("Se está pasando un parámetro incorrecto en un test de edición de datasets.")
        submit_and_follow(self.app, form, env, 'save', 'continue')
        return model.Package.by_name(dataset_name)

    def delete_package_using_forms(self, dataset_name):
        env, response = self.get_page_response(url_for('/dataset/delete/{0}'.format(dataset_name)), admin_required=True)
        form = response.forms['confirm-dataset-delete-form']
        response = submit_and_follow(self.app, form, env, 'delete')
        return response

    # --- Resources --- #

    def create_resource_using_forms(self, dataset_name, resource_name=u'resource'):
        env, response = self.get_page_response(str('/dataset/new_resource/%s' % dataset_name))
        form = response.forms['resource-edit']
        form['url'] = u'http://example.com/resource'
        form['name'] = resource_name
        submit_and_follow(self.app, form, env, 'save', 'go-dataset-complete')
        return model.Resource.by_name(resource_name)

    def delete_resource_using_forms(self, dataset_name, resource_id):
        url = url_for('/dataset/{0}/resource_delete/{1}'.format(dataset_name, resource_id))
        env, response = self.get_page_response(url)
        form = response.forms['confirm-resource-delete-form']
        try:
            response = submit_and_follow(self.app, form, env, 'delete')
        except sqlalchemy.exc.ProgrammingError:
            # Error subiendo al datastore
            pass
        return response

    # --- Datajson --- #

    def generate_datajson(self, cache_directory='/tmp/', cache_filename='/tmp/datajson_cache_test.json'):
        file_descriptor, file_path = tempfile.mkstemp(suffix='.json', dir=cache_directory)
        generate_new_cache_file(file_descriptor)
        os.rename(file_path, cache_filename)
        with open(cache_filename, 'r+') as file:
            return json.loads(file.read())

    # --- Portal --- #

    def return_value_to_default(self, url, form_name, field_name, value):
        # Restauro la información default, tal y como estaba antes de testear
        _, response = self.get_page_response(url_for(url), admin_required=True)
        self.edit_form_value(response, form_name, field_name, value)

    def edit_form_value(self, response, form_id=None, field_name=None, field_type='text', value=u'Campo modificado'):
        admin = factories.Sysadmin()
        if form_id:
            form = response.forms[form_id]
        else:
            # El form a buscar no tiene un id bajo el cual buscarlo
            form = response.forms[0]
        if field_type == 'text':
            form[field_name].value = value
        elif field_type == 'checkbox':
            form[field_name].checked = value
        env = {'REMOTE_USER': admin['name'].encode('ascii')}
        try:
            response = submit_and_follow(self.app, form, env, 'save', value="config-form")
        except Exception:
            # Trató de encolar una tarea
            pass
        return response

