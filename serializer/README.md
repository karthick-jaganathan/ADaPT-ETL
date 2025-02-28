## Adaptive Data Pipeline Toolkit - Serializer

`python` based, `config-powered` data pipeline for extracting. Leverage configuration to automate journey of data serialization to exporting to various locations, tailoring it to specific needs.


### Building and installing the package

`adapt_serializer` has dependency on following packages. Install them before proceeding further. 
You can find the installation instructions in the respective module `README.md` files.
 * `adapt_utils`

Following are the steps to build and install `adapt_serializer` package.

- clone the repo

  ```shell
  git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
  ```

- `cd` into the `ADaPT-ETL/serializer`

  ```shell
  cd ADaPT-ETL/serializer
  ```

- build and install python package

  ```shell
  python setup.py sdist && pip install dist/adapt_serializer-0.0.1.tar.gz
  ```

- verifying installation of the package

  ```shell
  pip show adapt_serializer
  ```

---

### Executing examples

- `cd` into the `ADaPT-ETL/serializer`

  ```shell
  cd ADaPT-ETL/serializer
  ```

- Executing JSON pipeline example (see: `examples/json_pipeline.py`

  ```shell
  python examples/json_pipeline.py
  ```

- Executing nested JSON dict normalization (see: `examples/dict_normalization.py`

  a) without normalization
    ```shell
    python examples/dict_normalization.py
    ```
  b) with normalization
    ```shell
    python examples/dict_normalization.py --dict-normalize
    ```

**Note**: make sure to install the package before executing the examples. 
See [Building and installing the package](#building-and-installing-the-package) section for more details.

---

### Defining `serializer` configurations

Let's say, an API returns a JSON response like below.

Assume, `latestValuation` is in millions.

```json
[
  {
    "id": 1,
    "companyName": "XYZ",
    "foundingYear": "2012",
    "marketSegment": "Technology",
    "fundingRound": "SERIES_B",
    "products": ["ProductA", "ProductB"],
    "latestValuation": 8000
  },
  {
    "id": 2,
    "companyName": "ABC",
    "founding_year": "2004",
    "marketSegment": "Healthcare",
    "fundingRound": "SERIES_E",
    "products": ["Product X", "Product Y"],
    "latestValuation": 20000
  },
  {
    "id": 3,
    "companyName": "LMN",
    "founding_year": "2022",
    "marketSegment": "Security",
    "fundingRound": "SEED",
    "products": ["Product M"],
    "latestValuation": 50
  }
]
```

And, We want to extract specific fields and transform the data from the 
API response and store it in a TSV file as below.

```tsv
Id    Name    Founding Year    Market Segment    Stage      Funding Round
1     XYZ     2012             Technology        Unicron    Series B
2     ABC     2004             Healthcare        Decacorn   Series E
3     LMN     2022             Security          Others     Seed
```


We can define a serializer config like below to achieve this.

```yaml
version: 0.1
kind: serializer

inline:
  - name: Id
    from: id
    transform:
      type: integer
      
  - name: Name
    from: companyName
    transform:
      type: string
      
  - name: Founding Year
    from: foundingYear
    transform:
      type: integer
    
  - name: Market Segment
    from: marketSegment
    transform:
      type: string

  - name: Stage
    transform:
      # latestValuation is in millions
      # so, let's assume that the company is a
      # 1. Decacorn, if the latestValuation is greater than 1000 million
      # 2. Unicorn, if the latestValuation is greater than 100 and less than 1000
      # 3. Others, if the latestValuation is less than 100
      type: case
      cases:
        - when:
            field: latestValuation
            greater_than: 10000  # 10 billion
          then: Decacorn
        - when:
            field: latestValuation
            greater_than: 1000  # 1 billion
          then: Unicorn
      default: Others

  - name: Funding Round
    from: fundingRound
    transform:
      type: enum
      mappings:
        SERIES_A: Series A
        SERIES_B: Series B
        SERIES_C: Series C
        SERIES_D: Series D
        SERIES_E: Series E
        SERIES_F: Series F
        SERIES_G: Series G
        SEED: Seed
        OTHERS: Others

# Export settings to store the data to a TSV file
export:
  type: tsv
  path: /tmp
  name: company_details
  fields:
    - Id
    - Name
    - Founding Year
    - Market Segment
    - Stage
    - Funding Round
```

We can then use the above config to extract and transform the data from the API response and store it in a TSV file as below.

```python
from adapt.serializer.serializer import Serializer
from adapt.utils.config_reader import YamlReader

# Read the serializer config
config = YamlReader.read('path/to/serializer/config.yaml')

# Create a serializer instance
serializer = Serializer.init(config)

data = [
    {
        "id": 1,
        "companyName": "XYZ",
        "foundingYear": "2012",
        "marketSegment": "Technology",
        "fundingRound": "SERIES_B",
        "products": ["ProductA", "ProductB"],
        "latestValuation": 8000
    },
    {
        "id": 2,
        "companyName": "ABC",
        "founding_year": "2004",
        "marketSegment": "Healthcare",
        "fundingRound": "SERIES_E",
        "products": ["Product X", "Product Y"],
        "latestValuation": 20000
    },
    {
        "id": 3,
        "companyName": "LMN",
        "founding_year": "2022",
        "marketSegment": "Security",
        "fundingRound": "SEED",
        "products": ["Product M"],
        "latestValuation": 50
    }
]

# Serialize the data
serialized_data = serializer.serialize_records(data)

for record in serialized_data:
    print(record)

```
