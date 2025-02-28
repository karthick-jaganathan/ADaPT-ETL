## Adaptive Data Pipeline Toolkit - Connector

`python` based, `config-powered` data pipeline for extracting. Leverage configuration to automate data extraction journey, tailoring it to specific needs.


### Building and installing the package

`adapt_connector` has dependency on following packages. Install them before proceeding further. 
You can find the installation instructions in the respective module `README.md` files.
 * `adapt_utils`

Following are the steps to build and install `adapt_connector` package.

- clone the repo

  ```shell
  git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
  ```

- `cd` into the `ADaPT-ETL/connector`

  ```shell
  cd ADaPT-ETL/connector
  ```

- build and install python package

  ```shell
  python setup.py sdist && pip install dist/adapt_connector-0.0.1.tar.gz
  ```

- verifying installation of the package

  ```shell
  pip show adapt_connector
  ```
